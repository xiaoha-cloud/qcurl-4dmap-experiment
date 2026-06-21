#!/usr/bin/env bash
# Fig.8-style combined sudden link-quality deterioration evaluation.
#
# Minimal storage defaults (override with env):
#   KEEP_PCAP=0  SAVE_OUTPUT_FLV=0  KEEP_RAW_RUNTIME=0  SAVE_VERBOSE_LOGS=0
#
# Usage (VM, repo root):
#   sudo env \
#     KEEP_PCAP=1 \
#     KEEP_RAW_RUNTIME=1 \
#     INPUT_FLV=/home/mininet/Videos/push_input.flv \
#     WORKER_PYTHON=/home/mininet/Project/qcurl-4dmap-experiment/.venv/bin/python3 \
#     ./scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh
#
# This runner is diagnostic: Phase 2 ownership, selected-path semantics and
# absolute-throughput interpretation are intentionally deferred.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REPO_ROOT="$ROOT"
CHECK_ONLY=0
if [[ "${1:-}" == "--check-only" ]]; then
  CHECK_ONLY=1
  shift
fi
if [[ "$#" -ne 0 ]]; then
  echo "[error] unsupported argument: $1" >&2
  exit 2
fi
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
FINALIZE="$ROOT/scripts/mininet/finalize_experiment_leg.sh"
PHASE2_STATE_DIR="${QACCESS_PHASE2_STATE_DIR:-$ROOT/derived}"
SCENARIO="${SCENARIO:-fig8}"
DETERIORATION_PROFILE="${DETERIORATION_PROFILE:-scripts/mininet/combined_deterioration_profile_90_150.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_VERBOSE_LOGS="${SAVE_VERBOSE_LOGS:-0}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"
BUFFER_SIZE="${QACCESS_RUNTIME_BUFFER_SIZE:-3000}"
MIN_SAMPLES_PER_PATH="${QACCESS_MIN_SAMPLES_PER_PATH:-1}"
MIN_SENDER_BYTE_DELTA="${QACCESS_MIN_SENDER_BYTE_DELTA:-1}"
WORKER_PYTHON="${WORKER_PYTHON:-${REPO_ROOT}/.venv/bin/python3}"
WORKER_MODEL="${QACCESS_WORKER_MODEL:-derived/qaccess_t_redesign/qaccess_t_model_delta_bw_1s.pkl}"
WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-derived/qaccess_t_redesign/qaccess_t_redesign_report.json}"
WORKER_TARGET_MODE="${QACCESS_WORKER_TARGET_MODE:-delta_bw_1s}"
EXECUTION_MODE="${QACCESS_EXECUTION_MODE:-shadow}"
MULTIPATH_SHADOW_SCORING=1
COOLDOWN_MS="${QACCESS_TRIGGER_COOLDOWN_MS:-60000}"
GATE_BPS="${QACCESS_MIN_DELTA_GAIN_BPS:-500000}"
GATE_MODE="${QACCESS_GATE_MODE:-absolute}"
MIN_RELATIVE_GAIN="${QACCESS_MIN_RELATIVE_GAIN:-0.03}"

resolve_repo_path() {
  local path="$1"
  if [[ "$path" = /* ]]; then
    printf '%s' "$path"
  else
    printf '%s/%s' "$ROOT" "$path"
  fi
}

WORKER_MODEL="$(resolve_repo_path "$WORKER_MODEL")"
WORKER_MODEL_METADATA="$(resolve_repo_path "$WORKER_MODEL_METADATA")"
DETERIORATION_PROFILE="$(resolve_repo_path "$DETERIORATION_PROFILE")"
PHASE2_STATE_DIR="$(resolve_repo_path "$PHASE2_STATE_DIR")"
[[ "$PHASE2_STATE_DIR" = /* ]] || { echo "[error] Phase 2 state dir must be absolute" >&2; exit 2; }
RUNTIME_COEFFS="$PHASE2_STATE_DIR/qaccess_t_runtime_coefficients.json"
TRIGGER_AUDIT_PATH="$PHASE2_STATE_DIR/qaccess_trigger_audit.jsonl"
ARCHIVE_DIR="$PHASE2_STATE_DIR/qaccess_processed_buffers"

case "$EXECUTION_MODE" in
  shadow|active) ;;
  *) echo "[error] QACCESS_EXECUTION_MODE must be shadow or active, got: $EXECUTION_MODE" >&2; exit 2 ;;
esac
case "$GATE_MODE" in
  absolute|relative|hybrid) ;;
  *) echo "[error] QACCESS_GATE_MODE must be absolute, relative, or hybrid, got: $GATE_MODE" >&2; exit 2 ;;
esac
if [[ "$WORKER_TARGET_MODE" != "delta_bw_1s" ]]; then
  echo "[error] this diagnostic runner requires target mode delta_bw_1s" >&2
  exit 2
fi

export KEEP_PCAP="${KEEP_PCAP:-0}"
export SAVE_OUTPUT_FLV="${SAVE_OUTPUT_FLV:-0}"
export KEEP_RAW_RUNTIME="${KEEP_RAW_RUNTIME:-0}"
# Candidate-score and request artifacts are required by the diagnostic validator.
export KEEP_ALL_PROCESSED_BUFFERS="${KEEP_ALL_PROCESSED_BUFFERS:-1}"
export THROUGHPUT_INTERVAL="${THROUGHPUT_INTERVAL:-1}"

WORKER_CMD=(
  "$WORKER_PYTHON" scripts/analyze/qaccess_t_update_worker.py
  --poll-interval 5
  --model "$WORKER_MODEL"
  --model-metadata "$WORKER_MODEL_METADATA"
  --target-mode "$WORKER_TARGET_MODE"
  --request "$PHASE2_STATE_DIR/qaccess_update_request.json"
  --runtime-samples "$PHASE2_STATE_DIR/qaccess_runtime_samples.csv"
  --coeffs-out "$PHASE2_STATE_DIR/qaccess_t_runtime_coefficients.json"
  --response-out "$PHASE2_STATE_DIR/qaccess_update_response.json"
  --state "$PHASE2_STATE_DIR/qaccess_worker_state.json"
  --archive-dir "$PHASE2_STATE_DIR/qaccess_processed_buffers"
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
WORKER_PID=""
WORKER_READY_TIMEOUT="${QACCESS_WORKER_READY_TIMEOUT:-30}"

validate_profile() {
  [[ -f "$DETERIORATION_PROFILE" ]] || { echo "[error] missing deterioration profile: $DETERIORATION_PROFILE" >&2; return 1; }
  awk '
    $1 == "IFACE=h2-eth1" { iface=1 }
    $1 == "0" && $2 == "20ms" && $3 == "0%" { first=1 }
    $1 == "90" && $2 == "80ms" && $3 == "0.05%" { second=1 }
    $1 == "150" && $2 == "20ms" && $3 == "0%" { third=1 }
    END { exit !(iface && first && second && third) }
  ' "$DETERIORATION_PROFILE" || {
    echo "[error] profile does not contain the required 0/90/150 second schedule: $DETERIORATION_PROFILE" >&2
    return 1
  }
}

check_configuration() {
  echo "[check] repository_root=$ROOT"
  echo "[check] input_media=${INPUT_FLV:-<mp_topo-default>}"
  [[ -n "$INPUT_FLV" && -f "$INPUT_FLV" ]] || { echo "[FAIL] input media is missing: ${INPUT_FLV:-<unset>}" >&2; return 1; }
  echo "[check] model_path=$WORKER_MODEL"
  [[ -f "$WORKER_MODEL" ]] || { echo "[FAIL] model is missing: $WORKER_MODEL" >&2; return 1; }
  echo "[check] model_exists=true"
  echo "[check] requested_target_mode=$WORKER_TARGET_MODE"
  echo "[check] deterioration_profile=$DETERIORATION_PROFILE"
  validate_profile
  echo "[check] execution_mode=$EXECUTION_MODE"
  echo "[check] buffer=$BUFFER_SIZE"
  echo "[check] cooldown_ms=$COOLDOWN_MS"
  echo "[check] gate_bps=$GATE_BPS"
  echo "[check] gate_mode=$GATE_MODE min_relative_gain=$MIN_RELATIVE_GAIN"
  [[ -x "$WORKER_PYTHON" ]] || { echo "[FAIL] worker Python is not executable: $WORKER_PYTHON" >&2; return 1; }
  "$WORKER_PYTHON" scripts/analyze/qaccess_t_update_worker.py \
    --model "$WORKER_MODEL" \
    --model-metadata "$WORKER_MODEL_METADATA" \
    --target-mode "$WORKER_TARGET_MODE" \
    --validate-model-only
  echo "[PASS] configuration is compatible; no Mininet, TC or runtime state was started"
}

if [[ "$CHECK_ONLY" == "1" ]]; then
  cd "$ROOT"
  check_configuration
  exit $?
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

if [[ -n "${SAVE_LOGS:-}" ]]; then
  echo "[combined_deterioration] note: legacy SAVE_LOGS is ignored; use SAVE_VERBOSE_LOGS=1 only when full role logs are required"
fi

cd "$ROOT"
mkdir -p derived logs_exp
chmod +x scripts/mininet/tc_deterioration_steps.sh scripts/mininet/finalize_experiment_leg.sh 2>/dev/null || true

SESSION_DIR="logs_exp/session_combined_deterioration_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"
SESSION_START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

stop_worker() {
  if [[ -n "${WORKER_PID:-}" ]] && kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "[combined_deterioration] stopping worker pid=$WORKER_PID"
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
  WORKER_PID=""
}

trap stop_worker EXIT

worker_ready_file() {
  printf '%s/worker_ready.json' "$SESSION_DIR"
}

worker_log_file() {
  printf '%s/worker.log' "$SESSION_DIR"
}

worker_process_log_file() {
  printf '%s/worker_process.log' "$SESSION_DIR"
}

preflight_worker_python() {
  local process_log="$1"
  echo "[combined_deterioration] worker python: $WORKER_PYTHON"
  if [[ ! -x "$WORKER_PYTHON" ]]; then
    echo "[error] WORKER_PYTHON is not executable: $WORKER_PYTHON" >&2
    return 1
  fi
  "$WORKER_PYTHON" --version 2>&1 | sed 's/^/[combined_deterioration] worker python version: /'
  "$WORKER_PYTHON" - "$ROOT" "$WORKER_MODEL" "$WORKER_MODEL_METADATA" "$PHASE2_STATE_DIR" >"$process_log" 2>&1 <<'PY'
import sys
from pathlib import Path

repo = Path(sys.argv[1])
model_path = Path(sys.argv[2])
metadata_path = Path(sys.argv[3])
state_dir = Path(sys.argv[4])
if not model_path.is_absolute():
    model_path = repo / model_path
if not metadata_path.is_absolute():
    metadata_path = repo / metadata_path
print(f"[worker-preflight] executable={sys.executable}", flush=True)
for mod_name in ("numpy", "pandas", "sklearn", "joblib"):
    mod = __import__(mod_name)
    print(f"[worker-preflight] import {mod_name}=ok version={getattr(mod, '__version__', '')}", flush=True)

import joblib

if not model_path.is_file():
    raise FileNotFoundError(f"missing model {model_path}")
joblib.load(model_path)
print(f"[worker-preflight] model_load=ok path={model_path}", flush=True)
if not metadata_path.is_file():
    raise FileNotFoundError(f"missing model metadata {metadata_path}")
print(f"[worker-preflight] model_metadata=ok path={metadata_path}", flush=True)

if not state_dir.is_absolute():
    raise ValueError(f"Phase 2 state dir must be absolute: {state_dir}")
coeffs_path = state_dir / "qaccess_t_runtime_coefficients.json"
if not coeffs_path.is_file():
    raise FileNotFoundError(f"missing runtime coefficients {coeffs_path}")
with coeffs_path.open("a", encoding="utf-8"):
    pass
print(f"[worker-preflight] coeffs_writable=ok path={coeffs_path}", flush=True)
PY
  local rc=$?
  if [[ "$rc" -ne 0 ]]; then
    echo "[error] worker Python preflight failed; see $process_log" >&2
    return "$rc"
  fi
  "$WORKER_PYTHON" scripts/analyze/qaccess_t_update_worker.py \
    --model "$WORKER_MODEL" \
    --model-metadata "$WORKER_MODEL_METADATA" \
    --target-mode "$WORKER_TARGET_MODE" \
    --validate-model-only >>"$process_log" 2>&1
}

wait_for_worker_ready() {
  local ready_file="$1"
  local deadline=$((SECONDS + WORKER_READY_TIMEOUT))
  while ((SECONDS <= deadline)); do
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
      echo "[error] worker exited before readiness marker; see $(worker_process_log_file)" >&2
      return 1
    fi
    if [[ -f "$ready_file" ]] && python3 - "$ready_file" "$WORKER_PID" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
want_pid = int(sys.argv[2])
try:
    doc = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(1)
if doc.get("status") != "ready":
    sys.exit(1)
if int(doc.get("pid", -1)) != want_pid:
    sys.exit(1)
sys.exit(0)
PY
    then
      echo "[combined_deterioration] worker ready: $ready_file"
      return 0
    fi
    sleep 1
  done
  echo "[error] worker readiness timed out after ${WORKER_READY_TIMEOUT}s; see $(worker_process_log_file)" >&2
  return 1
}

start_worker() {
  local ready_file
  local log_file
  local process_log
  ready_file="$(worker_ready_file)"
  log_file="$(worker_log_file)"
  process_log="$(worker_process_log_file)"
  rm -f "$ready_file"
  echo "[combined_deterioration] resolved worker model: $WORKER_MODEL"
  echo "[combined_deterioration] requested target mode: $WORKER_TARGET_MODE"
  echo "[combined_deterioration] execution mode: $EXECUTION_MODE"
  preflight_worker_python "$process_log"
  echo "[combined_deterioration] starting worker for dynamic leg"
  printf '[combined_deterioration] worker command:'
  printf ' %q' "${WORKER_CMD[@]}"
  printf ' --log-file %q --ready-file %q\n' "$log_file" "$ready_file"
  "${WORKER_CMD[@]}" --log-file "$log_file" --ready-file "$ready_file" >>"$process_log" 2>&1 &
  WORKER_PID="$!"
  wait_for_worker_ready "$ready_file"
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
    echo "[warn] post-processing failed for $label; continuing paired experiment" >&2
  fi
  if [[ -f "$leg_dir/leg_status.json" ]]; then
    python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
print('[finalize] leg={} experiment_completed={} postprocess_ok={} pcap_retained={}'.format(
    sys.argv[2], s.get('experiment_completed'), s.get('postprocess_ok'), s.get('pcap_retained')))
" "$leg_dir/leg_status.json" "$label"
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
  [[ "$SAVE_VERBOSE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[combined_deterioration] scenario=$SCENARIO utility-mode=$um label=$label profile=$DETERIORATION_PROFILE"
  echo "[combined_deterioration] KEEP_PCAP=$KEEP_PCAP SAVE_OUTPUT_FLV=$SAVE_OUTPUT_FLV KEEP_RAW_RUNTIME=$KEEP_RAW_RUNTIME SAVE_VERBOSE_LOGS=$SAVE_VERBOSE_LOGS"
  env "$@" "${cmd[@]}"
  finalize_leg "$label" || true
}

echo "[combined_deterioration] profile contents:"
validate_profile
cat "$DETERIORATION_PROFILE"
echo ""

echo "[combined_deterioration] validating worker model before starting Mininet"
echo "[combined_deterioration] resolved worker model: $WORKER_MODEL"
echo "[combined_deterioration] requested target mode: $WORKER_TARGET_MODE"
preflight_worker_python "$(worker_process_log_file)"

echo "[combined_deterioration] baseline leg (same deterioration profile, no qaccess, no worker)"
QACCESS_PHASE2_STATE_DIR="$PHASE2_STATE_DIR" bash "$RESET"
run_one baseline combined_baseline

echo "[combined_deterioration] reset runtime + initialize coefficients from initial"
QACCESS_PHASE2_STATE_DIR="$PHASE2_STATE_DIR" bash "$RESET"
COEFFS_BEFORE="$SESSION_DIR/combined_qaccess_t_dynamic_coeffs_before.json"
cp "$RUNTIME_COEFFS" "$COEFFS_BEFORE"
echo "[combined_deterioration] runtime coefficients BEFORE dynamic leg:"
cat "$COEFFS_BEFORE"
echo "[combined_deterioration] parsed: $(read_coeffs "$COEFFS_BEFORE")"

start_worker

if [[ "$EXECUTION_MODE" == "active" ]]; then
  echo "[combined_deterioration] active aggregate safety checks enabled; traffic-weighted aggregate controls updates"
fi
echo "[combined_deterioration] dynamic leg: qaccess_t + delta_bw_1s worker ($EXECUTION_MODE, gate_mode=$GATE_MODE absolute=${GATE_BPS}bps relative=$MIN_RELATIVE_GAIN)"
echo "[combined_deterioration] worker model=$WORKER_MODEL metadata=$WORKER_MODEL_METADATA"
echo "[combined_deterioration] global buffer capacity=$BUFFER_SIZE min samples per path=$MIN_SAMPLES_PER_PATH"
run_one qaccess_t combined_qaccess_t_dynamic \
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
  QACCESS_MULTIPATH_SHADOW_SCORING="$MULTIPATH_SHADOW_SCORING" \
  QACCESS_MIN_SENDER_BYTE_DELTA="$MIN_SENDER_BYTE_DELTA" \
  QACCESS_TRIGGER_AUDIT_JSONL="$TRIGGER_AUDIT_PATH"
stop_worker
python3 - "$(worker_log_file)" "$SESSION_DIR/dynamic_coefficient_timeline.jsonl" <<'PY'
import json, sys
from pathlib import Path
source, target = map(Path, sys.argv[1:3])
events = []
if source.is_file():
    for line in source.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append({key: row.get(key) for key in (
            "timestamp_ms", "request_id", "request_classification", "status", "gate_mode",
            "absolute_gain_bps", "relative_gain", "would_apply_under_gate", "actual_applied",
            "current_coefficients", "traffic_weighted_proposed_candidate",
            "traffic_weighted_proposed_stepped_coefficients", "applied_coefficients",
        )})
target.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in events), encoding="utf-8")
PY
if [[ -f "$TRIGGER_AUDIT_PATH" ]]; then
  cp "$TRIGGER_AUDIT_PATH" "$SESSION_DIR/qaccess_trigger_audit.jsonl"
fi
if [[ -f "$PHASE2_STATE_DIR/qaccess_owner_audit.jsonl" ]]; then
  cp "$PHASE2_STATE_DIR/qaccess_owner_audit.jsonl" "$SESSION_DIR/qaccess_owner_audit.jsonl"
fi

COEFFS_AFTER="$SESSION_DIR/combined_qaccess_t_dynamic_coeffs_after.json"
cp "$RUNTIME_COEFFS" "$COEFFS_AFTER"
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

SESSION_END="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 -c "
import json
from pathlib import Path
import subprocess

repo = Path('$ROOT')
session = repo / '$SESSION_DIR'
try:
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    branch = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo, text=True).strip()
except Exception:
    commit, branch = '', ''

def load(p):
    p = Path(p)
    return json.loads(p.read_text()) if p.is_file() else {}

meta = {
    'git_commit': commit,
    'branch': branch,
    'session_id': session.name,
    'start_time': '$SESSION_START',
    'end_time': '$SESSION_END',
    'control_law': __import__('os').environ.get('QACCESS_CONTROL_LAW', 'legacy'),
    'model_path': '$WORKER_MODEL',
    'model_metadata_path': '$WORKER_MODEL_METADATA',
    'target_mode': '$WORKER_TARGET_MODE',
    'gate_mode': '$GATE_MODE',
    'min_delta_gain_bps': float('$GATE_BPS'),
    'min_relative_gain': float('$MIN_RELATIVE_GAIN'),
    'execution_mode': '$EXECUTION_MODE',
    'worker_shadow': '$EXECUTION_MODE' == 'shadow',
    'initial_coefficients': load('$COEFFS_BEFORE'),
    'final_coefficients': load('$COEFFS_AFTER'),
    'profile_path': '$DETERIORATION_PROFILE',
    'timeout': int('$TIMEOUT'),
    'KEEP_PCAP': int('$KEEP_PCAP'),
    'KEEP_RAW_RUNTIME': int('$KEEP_RAW_RUNTIME'),
    'SAVE_OUTPUT_FLV': int('$SAVE_OUTPUT_FLV'),
    'legs': {},
}
for leg_name in ('combined_baseline', 'combined_qaccess_t_dynamic'):
    status_path = session / leg_name / 'leg_status.json'
    if status_path.is_file():
        meta['legs'][leg_name] = json.loads(status_path.read_text())
(session / 'experiment_metadata.json').write_text(json.dumps(meta, indent=2))
print('[combined_deterioration] wrote', session / 'experiment_metadata.json')
"

echo ""
echo "[combined_deterioration] session: $ROOT/$SESSION_DIR"
echo "[combined_deterioration] baseline:  $SESSION_DIR/combined_baseline"
echo "[combined_deterioration] dynamic:   $SESSION_DIR/combined_qaccess_t_dynamic"
echo "[combined_deterioration] worker log: $SESSION_DIR/worker.log"
echo "[combined_deterioration] worker process log: $SESSION_DIR/worker_process.log"
echo "[combined_deterioration] worker ready marker: $SESSION_DIR/worker_ready.json"
echo "[combined_deterioration] retained per leg: control_law_diagnostics.csv throughput_*_down.csv tc_deterioration.log (+ dynamic coeffs JSON at session root)"
