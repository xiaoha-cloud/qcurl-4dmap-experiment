#!/usr/bin/env bash
# Fig.8-style combined sudden link-quality deterioration evaluation.
#
# Minimal storage defaults (override with env):
#   KEEP_PCAP=0  SAVE_OUTPUT_FLV=0  KEEP_RAW_RUNTIME=0  SAVE_LOGS=0
#
# Usage (VM, repo root):
#   INPUT_FLV=~/Videos/push_input.flv \
#     sudo -E ./scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
FINALIZE="$ROOT/scripts/mininet/finalize_experiment_leg.sh"
RUNTIME_COEFFS="${QACCESS_COEFFS_JSON:-derived/qaccess_t_runtime_coefficients.json}"
SCENARIO="${SCENARIO:-fig8}"
DETERIORATION_PROFILE="${DETERIORATION_PROFILE:-scripts/mininet/combined_deterioration_profile.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_LOGS="${SAVE_LOGS:-0}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"
BUFFER_SIZE="${QACCESS_RUNTIME_BUFFER_SIZE:-3000}"
ARCHIVE_DIR="${QACCESS_ARCHIVE_DIR:-derived/qaccess_processed_buffers}"

export KEEP_PCAP="${KEEP_PCAP:-0}"
export SAVE_OUTPUT_FLV="${SAVE_OUTPUT_FLV:-0}"
export KEEP_RAW_RUNTIME="${KEEP_RAW_RUNTIME:-0}"
export KEEP_ALL_PROCESSED_BUFFERS="${KEEP_ALL_PROCESSED_BUFFERS:-0}"
export THROUGHPUT_INTERVAL="${THROUGHPUT_INTERVAL:-1}"

WORKER_CMD=(
  python3 scripts/analyze/qaccess_t_update_worker.py
  --poll-interval 5
  --model derived/qaccess_t_model.pkl
  --coeffs-out derived/qaccess_t_runtime_coefficients.json
  --min-improvement-pct 1.0
)
WORKER_PID=""
WORKER_READY_TIMEOUT="${QACCESS_WORKER_READY_TIMEOUT:-30}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
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
  echo "[combined_deterioration] starting worker for dynamic leg"
  printf '[combined_deterioration] worker command:'
  printf ' %q' "${WORKER_CMD[@]}"
  printf ' --log-file %q --ready-file %q\n' "$log_file" "$ready_file"
  "${WORKER_CMD[@]}" --log-file "$log_file" --ready-file "$ready_file" >"$process_log" 2>&1 &
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
  if [[ -d "$ROOT/$ARCHIVE_DIR" ]]; then
    cp -a "$ROOT/$ARCHIVE_DIR/." "$dest_dir/processed_buffers/" 2>/dev/null || true
  fi
  for rel in \
    "derived/qaccess_runtime_samples.csv" \
    "derived/qaccess_update_response.json" \
    "$RUNTIME_COEFFS"; do
    if [[ -f "$ROOT/$rel" ]]; then
      cp "$ROOT/$rel" "$dest_dir/derived_snapshots/$(basename "$rel")"
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
  [[ "$SAVE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[combined_deterioration] scenario=$SCENARIO utility-mode=$um label=$label profile=$DETERIORATION_PROFILE"
  echo "[combined_deterioration] KEEP_PCAP=$KEEP_PCAP SAVE_OUTPUT_FLV=$SAVE_OUTPUT_FLV KEEP_RAW_RUNTIME=$KEEP_RAW_RUNTIME"
  env "$@" "${cmd[@]}"
  finalize_leg "$label" || true
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

start_worker

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
stop_worker

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
    'model_path': 'derived/qaccess_t_model.pkl',
    'target_mode': 'delta_bw_1s',
    'gate_threshold': 'min_improvement_pct=1.0',
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
