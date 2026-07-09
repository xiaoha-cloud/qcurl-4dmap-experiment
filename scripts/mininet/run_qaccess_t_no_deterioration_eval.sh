#!/usr/bin/env bash
# Q-ACCeSS-T no-deterioration evaluation.
# Runs baseline and Q-ACCeSS-T on the static Fig.7 topology without dynamic
# bandwidth, delay, loss, or deterioration profiles.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REPO_ROOT="$ROOT"
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
FINALIZE="$ROOT/scripts/mininet/finalize_experiment_leg.sh"
EVAL="$ROOT/scripts/analyze/evaluate_qaccess_t_no_deterioration.py"

CHECK_ONLY=0
if [[ "${1:-}" == "--check-only" ]]; then
  CHECK_ONLY=1
  shift
fi
if [[ "$#" -ne 0 ]]; then
  echo "[error] unsupported argument: $1" >&2
  exit 2
fi

SCENARIO="${SCENARIO:-fig7}"
TIMEOUT="${TIMEOUT:-220}"
POST_UPDATE_OBSERVE_SEC="${QACCESS_POST_UPDATE_OBSERVE_SEC:-15}"
INPUT_FLV="${INPUT_FLV:-}"
SAVE_VERBOSE_LOGS="${SAVE_VERBOSE_LOGS:-1}"
LOG_CONTROL="${LOG_CONTROL:-0}"
PHASE2_STATE_DIR="${QACCESS_PHASE2_STATE_DIR:-$ROOT/derived}"
BUFFER_SIZE="${QACCESS_RUNTIME_BUFFER_SIZE:-3000}"
MIN_SAMPLES_PER_PATH="${QACCESS_MIN_SAMPLES_PER_PATH:-1}"
MIN_SENDER_BYTE_DELTA="${QACCESS_MIN_SENDER_BYTE_DELTA:-1}"
COOLDOWN_MS="${QACCESS_TRIGGER_COOLDOWN_MS:-60000}"
WORKER_PYTHON="${WORKER_PYTHON:-${REPO_ROOT}/.venv/bin/python3}"
WORKER_MODEL="${QACCESS_WORKER_MODEL:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_model_delta_bw_1s.pkl}"
WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_qserver_sender_report.json}"
WORKER_TARGET_MODE="${QACCESS_WORKER_TARGET_MODE:-delta_bw_1s}"
EXECUTION_MODE="${QACCESS_EXECUTION_MODE:-active}"
GATE_MODE="${QACCESS_GATE_MODE:-absolute}"
GATE_BPS="${QACCESS_GATE_BPS:-500000}"
MIN_RELATIVE_GAIN="${QACCESS_MIN_RELATIVE_GAIN:-0.03}"
EVAL_PYTHON="${EVAL_PYTHON:-$WORKER_PYTHON}"

case "$EXECUTION_MODE" in
  active|shadow) ;;
  *) echo "[error] QACCESS_EXECUTION_MODE must be active or shadow, got: $EXECUTION_MODE" >&2; exit 2 ;;
esac
case "$WORKER_TARGET_MODE" in
  delta_bw_1s) ;;
  *) echo "[error] no-deterioration Q-ACCeSS-T runner requires target mode delta_bw_1s" >&2; exit 2 ;;
esac
case "$GATE_MODE" in
  absolute|relative|hybrid) ;;
  *) echo "[error] QACCESS_GATE_MODE must be absolute, relative, or hybrid, got: $GATE_MODE" >&2; exit 2 ;;
esac

export KEEP_PCAP="${KEEP_PCAP:-1}"
export SAVE_OUTPUT_FLV="${SAVE_OUTPUT_FLV:-1}"
export KEEP_RAW_RUNTIME="${KEEP_RAW_RUNTIME:-1}"
export KEEP_ALL_PROCESSED_BUFFERS="${KEEP_ALL_PROCESSED_BUFFERS:-1}"
export THROUGHPUT_INTERVAL="${THROUGHPUT_INTERVAL:-1}"
export QACCESS_ENABLE_QOE_LOG="${QACCESS_ENABLE_QOE_LOG:-1}"
export QACCESS_QOE_LOG_VIDEO_EVERY_N="${QACCESS_QOE_LOG_VIDEO_EVERY_N:-1}"
export QACCESS_QOE_LOG_AUDIO="${QACCESS_QOE_LOG_AUDIO:-0}"

RUNTIME_COEFFS="$PHASE2_STATE_DIR/qaccess_t_runtime_coefficients.json"
TRIGGER_AUDIT_PATH="$PHASE2_STATE_DIR/qaccess_trigger_audit.jsonl"
ARCHIVE_DIR="$PHASE2_STATE_DIR/qaccess_processed_buffers"

WORKER_CMD=(
  "$WORKER_PYTHON" scripts/analyze/qaccess_t_update_worker.py
  --poll-interval 5
  --model "$WORKER_MODEL"
  --model-metadata "$WORKER_MODEL_METADATA"
  --target-mode "$WORKER_TARGET_MODE"
  --request "$PHASE2_STATE_DIR/qaccess_update_request.json"
  --runtime-samples "$PHASE2_STATE_DIR/qaccess_runtime_samples.csv"
  --coeffs-out "$RUNTIME_COEFFS"
  --response-out "$PHASE2_STATE_DIR/qaccess_update_response.json"
  --state "$PHASE2_STATE_DIR/qaccess_worker_state.json"
  --archive-dir "$ARCHIVE_DIR"
  --audit-csv "$PHASE2_STATE_DIR/qaccess_update_audit.csv"
  --min-delta-gain-bps "$GATE_BPS"
  --gate-mode "$GATE_MODE"
  --min-relative-gain "$MIN_RELATIVE_GAIN"
  --min-sender-byte-delta "$MIN_SENDER_BYTE_DELTA"
  --aggregate-multipath
)
if [[ "$EXECUTION_MODE" == "shadow" ]]; then
  WORKER_CMD+=(--shadow-per-subflow)
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
  cd "$ROOT"
  echo "[check] repository_root=$ROOT"
  echo "[check] scenario=$SCENARIO"
  echo "[check] dynamic_profile=none"
  echo "[check] input_media=${INPUT_FLV:-<mp_topo-default>}"
  echo "[check] model_path=$WORKER_MODEL"
  echo "[check] model_metadata=$WORKER_MODEL_METADATA"
  echo "[check] worker_python=$WORKER_PYTHON"
  echo "[check] KEEP_PCAP=$KEEP_PCAP SAVE_OUTPUT_FLV=$SAVE_OUTPUT_FLV QACCESS_ENABLE_QOE_LOG=$QACCESS_ENABLE_QOE_LOG"
  [[ -f "$MP" ]] || { echo "[FAIL] missing mp_topo.py: $MP" >&2; exit 1; }
  [[ -f "$EVAL" ]] || { echo "[FAIL] missing evaluator: $EVAL" >&2; exit 1; }
  [[ -f "$WORKER_MODEL" ]] || { echo "[FAIL] model is missing: $WORKER_MODEL" >&2; exit 1; }
  [[ -f "$WORKER_MODEL_METADATA" ]] || { echo "[FAIL] model metadata is missing: $WORKER_MODEL_METADATA" >&2; exit 1; }
  [[ -x "$WORKER_PYTHON" ]] || { echo "[FAIL] worker python is not executable: $WORKER_PYTHON" >&2; exit 1; }
  if [[ -n "$INPUT_FLV" && ! -f "$INPUT_FLV" ]]; then
    echo "[FAIL] input media is missing: $INPUT_FLV" >&2
    exit 1
  fi
  "$WORKER_PYTHON" scripts/analyze/qaccess_t_update_worker.py \
    --model "$WORKER_MODEL" \
    --model-metadata "$WORKER_MODEL_METADATA" \
    --target-mode "$WORKER_TARGET_MODE" \
    --validate-model-only
  "$EVAL_PYTHON" "$EVAL" --help >/dev/null
  echo "[PASS] no-deterioration configuration is compatible; no Mininet state was started"
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived logs_exp
SESSION_DIR="logs_exp/session_qaccess_t_no_deterioration_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > logs_exp/.last_session
SESSION_START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
WORKER_PID=""

worker_ready_file() { printf '%s/worker_ready.json' "$SESSION_DIR"; }
worker_log_file() { printf '%s/worker.log' "$SESSION_DIR"; }
worker_process_log_file() { printf '%s/worker_process.log' "$SESSION_DIR"; }

stop_worker() {
  if [[ -n "${WORKER_PID:-}" ]] && kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "[no_deterioration] stopping worker pid=$WORKER_PID"
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
  WORKER_PID=""
}
trap stop_worker EXIT INT TERM

preflight_worker_python() {
  local process_log="$1"
  echo "[no_deterioration] worker python: $WORKER_PYTHON"
  [[ -x "$WORKER_PYTHON" ]] || { echo "[error] worker python is not executable: $WORKER_PYTHON" >&2; return 1; }
  "$WORKER_PYTHON" --version 2>&1 | sed 's/^/[no_deterioration] worker python version: /'
  "$WORKER_PYTHON" scripts/analyze/qaccess_t_update_worker.py \
    --model "$WORKER_MODEL" \
    --model-metadata "$WORKER_MODEL_METADATA" \
    --target-mode "$WORKER_TARGET_MODE" \
    --validate-model-only >"$process_log" 2>&1
}

wait_for_worker_ready() {
  local ready_file="$1"
  local deadline=$((SECONDS + ${QACCESS_WORKER_READY_TIMEOUT:-30}))
  while ((SECONDS <= deadline)); do
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
      echo "[error] worker exited before readiness marker; see $(worker_process_log_file)" >&2
      return 1
    fi
    if [[ -f "$ready_file" ]] && python3 - "$ready_file" "$WORKER_PID" <<'PYREADY'
import json, sys
from pathlib import Path
row = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
sys.exit(0 if row.get('status') == 'ready' and int(row.get('pid', -1)) == int(sys.argv[2]) else 1)
PYREADY
    then
      echo "[no_deterioration] worker ready: $ready_file"
      return 0
    fi
    sleep 1
  done
  echo "[error] worker readiness timed out" >&2
  return 1
}

start_worker() {
  local ready_file process_log log_file
  ready_file="$(worker_ready_file)"
  process_log="$(worker_process_log_file)"
  log_file="$(worker_log_file)"
  rm -f "$ready_file"
  preflight_worker_python "$process_log"
  echo "[no_deterioration] starting worker for Q-ACCeSS-T leg"
  "${WORKER_CMD[@]}" --log-file "$log_file" --ready-file "$ready_file" >>"$process_log" 2>&1 &
  WORKER_PID="$!"
  wait_for_worker_ready "$ready_file"
}

sync_leg_inputs() {
  local dest_dir="$1"
  mkdir -p "$dest_dir/processed_buffers" "$dest_dir/derived_snapshots"
  if [[ -d "$ARCHIVE_DIR" ]]; then
    cp -a "$ARCHIVE_DIR/." "$dest_dir/processed_buffers/" 2>/dev/null || true
  fi
  for rel in \
    "$PHASE2_STATE_DIR/qaccess_runtime_samples.csv" \
    "$PHASE2_STATE_DIR/qaccess_update_response.json" \
    "$RUNTIME_COEFFS"; do
    if [[ -f "$rel" ]]; then
      cp "$rel" "$dest_dir/derived_snapshots/$(basename "$rel")"
    fi
  done
}

finalize_leg() {
  local label="$1"
  local leg_dir="$SESSION_DIR/$label"
  sync_leg_inputs "$leg_dir"
  if bash "$FINALIZE" "$leg_dir" "$label"; then
    echo "[finalize] leg=$label status=ok"
  else
    echo "[warn] post-processing failed for $label; continuing" >&2
  fi
}

run_one() {
  local um="$1"
  local label="$2"
  local run_timeout="$3"
  shift 3
  local -a cmd=(
    python3 "$MP" --run-exp --scenario "$SCENARIO" --utility-mode "$um"
    --timeout "$run_timeout" --log-parent "$SESSION_DIR" --run-label "$label"
  )
  [[ "$SAVE_VERBOSE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[no_deterioration] scenario=$SCENARIO utility-mode=$um label=$label dynamic_profile=none"
  echo "[no_deterioration] KEEP_PCAP=$KEEP_PCAP SAVE_OUTPUT_FLV=$SAVE_OUTPUT_FLV QACCESS_ENABLE_QOE_LOG=$QACCESS_ENABLE_QOE_LOG SAVE_VERBOSE_LOGS=$SAVE_VERBOSE_LOGS"
  env "$@" "${cmd[@]}"
  finalize_leg "$label" || true
}

echo "[no_deterioration] static topology: Path A 20Mbps/40ms/0%, Path B 20Mbps/20ms/0.001%"
echo "[no_deterioration] no dynamic bandwidth, delay, loss, or deterioration profile will be used"
echo "[no_deterioration] validating worker model"
preflight_worker_python "$(worker_process_log_file)"

echo "[no_deterioration] baseline leg"
QACCESS_PHASE2_STATE_DIR="$PHASE2_STATE_DIR" bash "$RESET"
run_one baseline no_deterioration_baseline "$TIMEOUT"

echo "[no_deterioration] reset runtime + initialize coefficients"
QACCESS_PHASE2_STATE_DIR="$PHASE2_STATE_DIR" bash "$RESET"
COEFFS_BEFORE="$SESSION_DIR/no_deterioration_qaccess_t_coeffs_before.json"
cp "$RUNTIME_COEFFS" "$COEFFS_BEFORE" 2>/dev/null || true
start_worker

Q_TIMEOUT="$TIMEOUT"
if [[ "$EXECUTION_MODE" == "active" ]]; then
  Q_TIMEOUT=$((TIMEOUT + POST_UPDATE_OBSERVE_SEC))
fi
echo "[no_deterioration] Q-ACCeSS-T leg execution_mode=$EXECUTION_MODE gate_mode=$GATE_MODE gate_bps=$GATE_BPS"
run_one qaccess_t no_deterioration_qaccess_t "$Q_TIMEOUT" \
  QACCESS_PHASE2_STATE_DIR="$PHASE2_STATE_DIR" \
  QACCESS_COEFFS_JSON="$RUNTIME_COEFFS" \
  QACCESS_COEFF_RELOAD=1 \
  QACCESS_TRIGGER_UPDATE=1 \
  QACCESS_RUNTIME_SAMPLE_EXPORT=1 \
  QACCESS_TRIGGER_ON_BUFFER_FULL=1 \
  QACCESS_TRIGGER_ON_THROUGHPUT_DROP=0 \
  QACCESS_TRIGGER_PERIODIC_MS=0 \
  QACCESS_TRIGGER_COOLDOWN_MS="$COOLDOWN_MS" \
  QACCESS_RUNTIME_BUFFER_SIZE="$BUFFER_SIZE" \
  QACCESS_MIN_SAMPLES_PER_PATH="$MIN_SAMPLES_PER_PATH" \
  QACCESS_MIN_SENDER_BYTE_DELTA="$MIN_SENDER_BYTE_DELTA" \
  QACCESS_TRIGGER_AUDIT_JSONL="$TRIGGER_AUDIT_PATH"

stop_worker
trap - EXIT INT TERM

if [[ -f "$TRIGGER_AUDIT_PATH" ]]; then
  cp "$TRIGGER_AUDIT_PATH" "$SESSION_DIR/qaccess_trigger_audit.jsonl"
fi
if [[ -f "$PHASE2_STATE_DIR/qaccess_owner_audit.jsonl" ]]; then
  cp "$PHASE2_STATE_DIR/qaccess_owner_audit.jsonl" "$SESSION_DIR/qaccess_owner_audit.jsonl"
fi
COEFFS_AFTER="$SESSION_DIR/no_deterioration_qaccess_t_coeffs_after.json"
cp "$RUNTIME_COEFFS" "$COEFFS_AFTER" 2>/dev/null || true

SESSION_END="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - <<PYMETA
import json, subprocess
from pathlib import Path
repo = Path('$ROOT')
session = repo / '$SESSION_DIR'
meta = {
    'session_id': session.name,
    'experiment': 'qaccess_t_no_deterioration',
    'start_time': '$SESSION_START',
    'end_time': '$SESSION_END',
    'scenario': '$SCENARIO',
    'dynamic_profile': None,
    'path_a': {'bandwidth_mbps': 20, 'delay_ms': 40, 'loss_pct': 0.0},
    'path_b': {'bandwidth_mbps': 20, 'delay_ms': 20, 'loss_pct': 0.001},
    'input_flv': '$INPUT_FLV',
    'timeout': int('$TIMEOUT'),
    'qaccess_t_timeout': int('$Q_TIMEOUT'),
    'execution_mode': '$EXECUTION_MODE',
    'gate_mode': '$GATE_MODE',
    'gate_bps': float('$GATE_BPS'),
    'worker_model': '$WORKER_MODEL',
    'worker_model_metadata': '$WORKER_MODEL_METADATA',
    'KEEP_PCAP': int('$KEEP_PCAP'),
    'SAVE_OUTPUT_FLV': int('$SAVE_OUTPUT_FLV'),
    'QACCESS_ENABLE_QOE_LOG': int('$QACCESS_ENABLE_QOE_LOG'),
}
try:
    meta['git_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    meta['branch'] = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo, text=True).strip()
except Exception:
    pass
(session / 'experiment_metadata.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
print('[no_deterioration] wrote', session / 'experiment_metadata.json')
PYMETA

echo "[no_deterioration] running evaluation"
if [[ -n "$INPUT_FLV" ]]; then
  "$EVAL_PYTHON" "$EVAL" --session "$SESSION_DIR" --out "$SESSION_DIR/evaluation_no_deterioration" --input-flv "$INPUT_FLV"
else
  "$EVAL_PYTHON" "$EVAL" --session "$SESSION_DIR" --out "$SESSION_DIR/evaluation_no_deterioration"
fi

echo "[no_deterioration] session: $ROOT/$SESSION_DIR"
echo "[no_deterioration] evaluation: $ROOT/$SESSION_DIR/evaluation_no_deterioration"
echo "[no_deterioration] baseline: $SESSION_DIR/no_deterioration_baseline"
echo "[no_deterioration] qaccess_t: $SESSION_DIR/no_deterioration_qaccess_t"
