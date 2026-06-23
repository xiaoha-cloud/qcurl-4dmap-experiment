#!/usr/bin/env bash
# Fig.7-style capacity-change evaluation: baseline vs dynamic Q-ACCeSS-T hybrid gate.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REPO_ROOT="$ROOT"
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
FINALIZE="$ROOT/scripts/mininet/finalize_experiment_leg.sh"

CHECK_ONLY=0
if [[ "${1:-}" == "--check-only" ]]; then
  CHECK_ONLY=1
  shift
fi
if [[ "$#" -ne 0 ]]; then
  echo "[error] unsupported argument: $1" >&2
  exit 2
fi

SCENARIO="fig7"
BW_PROFILE="${BW_PROFILE:-$ROOT/scripts/mininet/bw_profile.fig7_200s.env}"
TIMEOUT="${TIMEOUT:-220}"
POST_UPDATE_OBSERVE_SEC="${QACCESS_POST_UPDATE_OBSERVE_SEC:-15}"
INPUT_FLV="${INPUT_FLV:-}"
SAVE_VERBOSE_LOGS="${SAVE_VERBOSE_LOGS:-0}"
LOG_CONTROL="${LOG_CONTROL:-0}"

PHASE2_STATE_DIR="${QACCESS_PHASE2_STATE_DIR:-$ROOT/derived}"
BUFFER_SIZE="${QACCESS_RUNTIME_BUFFER_SIZE:-3000}"
MIN_SAMPLES_PER_PATH="${QACCESS_MIN_SAMPLES_PER_PATH:-1}"
MIN_SENDER_BYTE_DELTA="${QACCESS_MIN_SENDER_BYTE_DELTA:-1}"
COOLDOWN_MS="${QACCESS_TRIGGER_COOLDOWN_MS:-60000}"
CHANGED_PATH_PRIORITY_SHADOW="${QACCESS_CHANGED_PATH_PRIORITY_SHADOW:-1}"
CHANGED_PATH_IDS="${QACCESS_CHANGED_PATH_IDS:-3}"
CHANGED_PATH_GAIN_BPS="${QACCESS_CHANGED_PATH_GAIN_BPS:-100000}"
MIN_AGGREGATE_GAIN_BPS="${QACCESS_MIN_AGGREGATE_GAIN_BPS:-0}"
MAX_OTHER_PATH_LOSS_RATIO="${QACCESS_MAX_OTHER_PATH_LOSS_RATIO:-0.75}"
MAX_OTHER_PATH_LOSS_BPS="${QACCESS_MAX_OTHER_PATH_LOSS_BPS:-200000}"

WORKER_PYTHON="${WORKER_PYTHON:-${REPO_ROOT}/.venv/bin/python3}"
WORKER_MODEL="${QACCESS_WORKER_MODEL:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_model_delta_bw_1s.pkl}"
WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_qserver_sender_report.json}"
WORKER_TARGET_MODE="${QACCESS_WORKER_TARGET_MODE:-delta_bw_1s}"
EXECUTION_MODE="${QACCESS_EXECUTION_MODE:-active}"
GATE_MODE="${QACCESS_GATE_MODE:-hybrid}"
MIN_RELATIVE_GAIN="${QACCESS_MIN_RELATIVE_GAIN:-0.03}"
GATE_BPS="${QACCESS_GATE_BPS:-100000}"
MULTIPATH_SHADOW_SCORING=1
WORKER_READY_TIMEOUT="${QACCESS_WORKER_READY_TIMEOUT:-30}"

[[ "$PHASE2_STATE_DIR" = /* ]] || { echo "[error] QACCESS_PHASE2_STATE_DIR must be absolute: $PHASE2_STATE_DIR" >&2; exit 2; }
[[ -f "$BW_PROFILE" ]] || { echo "[error] missing bandwidth profile: $BW_PROFILE" >&2; exit 2; }

case "$EXECUTION_MODE" in
  active|shadow) ;;
  *) echo "[error] QACCESS_EXECUTION_MODE must be active or shadow, got: $EXECUTION_MODE" >&2; exit 2 ;;
esac
case "$GATE_MODE" in
  hybrid|relative|absolute) ;;
  *) echo "[error] QACCESS_GATE_MODE must be hybrid, relative, or absolute, got: $GATE_MODE" >&2; exit 2 ;;
esac
if [[ "$WORKER_TARGET_MODE" != "delta_bw_1s" ]]; then
  echo "[error] this Fig.7 runner requires target mode delta_bw_1s" >&2
  exit 2
fi

RUNTIME_COEFFS="$PHASE2_STATE_DIR/qaccess_t_runtime_coefficients.json"
TRIGGER_AUDIT_PATH="$PHASE2_STATE_DIR/qaccess_trigger_audit.jsonl"
ARCHIVE_DIR="$PHASE2_STATE_DIR/qaccess_processed_buffers"

export KEEP_PCAP="${KEEP_PCAP:-0}"
export SAVE_OUTPUT_FLV="${SAVE_OUTPUT_FLV:-0}"
export KEEP_RAW_RUNTIME="${KEEP_RAW_RUNTIME:-0}"
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
if [[ "$CHANGED_PATH_PRIORITY_SHADOW" == "1" ]]; then
  WORKER_CMD+=(
    --changed-path-priority-shadow
    --changed-path-gain-bps "$CHANGED_PATH_GAIN_BPS"
    --min-aggregate-gain-bps "$MIN_AGGREGATE_GAIN_BPS"
    --max-other-path-loss-ratio "$MAX_OTHER_PATH_LOSS_RATIO"
    --max-other-path-loss-bps "$MAX_OTHER_PATH_LOSS_BPS"
  )
  # shellcheck disable=SC2206
  changed_ids=( $CHANGED_PATH_IDS )
  if [[ "${#changed_ids[@]}" -gt 0 ]]; then
    WORKER_CMD+=(--changed-path-ids "${changed_ids[@]}")
  fi
fi
if [[ "$EXECUTION_MODE" == "shadow" ]]; then
  WORKER_CMD+=(--shadow-per-subflow)
fi

worker_ready_file() { printf '%s/worker_ready.json' "$SESSION_DIR"; }
worker_log_file() { printf '%s/worker.log' "$SESSION_DIR"; }
worker_process_log_file() { printf '%s/worker_process.log' "$SESSION_DIR"; }

preflight_worker_python() {
  local process_log="$1"
  echo "[fig7_hybrid] worker python: $WORKER_PYTHON"
  [[ -x "$WORKER_PYTHON" ]] || { echo "[error] worker python is not executable: $WORKER_PYTHON" >&2; return 1; }
  "$WORKER_PYTHON" --version 2>&1 | sed 's/^/[fig7_hybrid] worker python version: /'
  "$WORKER_PYTHON" scripts/analyze/qaccess_t_update_worker.py \
    --model "$WORKER_MODEL" \
    --model-metadata "$WORKER_MODEL_METADATA" \
    --target-mode "$WORKER_TARGET_MODE" \
    --validate-model-only >"$process_log" 2>&1
}

stop_worker() {
  if [[ -n "${WORKER_PID:-}" ]] && kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "[fig7_hybrid] stopping worker pid=$WORKER_PID"
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
  WORKER_PID=""
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
import json, sys
from pathlib import Path
doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sys.exit(0 if doc.get("status") == "ready" and int(doc.get("pid", -1)) == int(sys.argv[2]) else 1)
PY
    then
      echo "[fig7_hybrid] worker ready: $ready_file"
      return 0
    fi
    sleep 1
  done
  echo "[error] worker readiness timed out after ${WORKER_READY_TIMEOUT}s; see $(worker_process_log_file)" >&2
  return 1
}

start_worker() {
  local ready_file process_log log_file
  ready_file="$(worker_ready_file)"
  process_log="$(worker_process_log_file)"
  log_file="$(worker_log_file)"
  rm -f "$ready_file"
  preflight_worker_python "$process_log"
  echo "[fig7_hybrid] starting worker for dynamic leg"
  printf '[fig7_hybrid] worker command:'
  printf ' %q' "${WORKER_CMD[@]}"
  printf ' --log-file %q --ready-file %q\n' "$log_file" "$ready_file"
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
    echo "[warn] post-processing failed for $label; continuing experiment" >&2
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
    --dynamic-bw-profile "$BW_PROFILE"
  )
  [[ "$SAVE_VERBOSE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[fig7_hybrid] scenario=$SCENARIO utility-mode=$um label=$label profile=$BW_PROFILE"
  echo "[fig7_hybrid] KEEP_PCAP=$KEEP_PCAP SAVE_OUTPUT_FLV=$SAVE_OUTPUT_FLV KEEP_RAW_RUNTIME=$KEEP_RAW_RUNTIME SAVE_VERBOSE_LOGS=$SAVE_VERBOSE_LOGS"
  env "$@" "${cmd[@]}"
  finalize_leg "$label" || true
}

if [[ "$CHECK_ONLY" == "1" ]]; then
  cd "$ROOT"
  mkdir -p "$ROOT/logs_exp/validation_logs"
  echo "[check] repository_root=$ROOT"
  echo "[check] input_media=${INPUT_FLV:-<mp_topo-default>}"
  echo "[check] model_path=$WORKER_MODEL"
  echo "[check] bandwidth_profile=$BW_PROFILE"
  echo "[check] execution_mode=$EXECUTION_MODE"
  echo "[check] gate_mode=$GATE_MODE min_relative_gain=$MIN_RELATIVE_GAIN min_delta_gain_bps=$GATE_BPS"
  echo "[check] changed_path_priority_shadow=$CHANGED_PATH_PRIORITY_SHADOW changed_path_ids=$CHANGED_PATH_IDS"
  echo "[check] buffer=$BUFFER_SIZE cooldown_ms=$COOLDOWN_MS"
  [[ -f "$WORKER_MODEL" ]] || { echo "[FAIL] model is missing: $WORKER_MODEL" >&2; exit 1; }
  [[ -f "$WORKER_MODEL_METADATA" ]] || { echo "[FAIL] model metadata is missing: $WORKER_MODEL_METADATA" >&2; exit 1; }
  [[ -x "$WORKER_PYTHON" ]] || { echo "[FAIL] worker python is not executable: $WORKER_PYTHON" >&2; exit 1; }
  preflight_worker_python "$ROOT/logs_exp/validation_logs/fig7_hybrid_check.log"
  echo "[PASS] configuration is compatible; no Mininet or runtime state was started"
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived logs_exp

SESSION_DIR="logs_exp/session_fig7_capacity_hybrid_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"
SESSION_START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
WORKER_PID=""
trap stop_worker EXIT

echo "[fig7_hybrid] profile contents:"
cat "$BW_PROFILE"
echo ""

echo "[fig7_hybrid] validating worker model before starting Mininet"
preflight_worker_python "$(worker_process_log_file)"

echo "[fig7_hybrid] baseline leg"
QACCESS_PHASE2_STATE_DIR="$PHASE2_STATE_DIR" bash "$RESET"
run_one baseline fig7_baseline "$TIMEOUT"

echo "[fig7_hybrid] reset runtime + initialize coefficients from initial"
QACCESS_PHASE2_STATE_DIR="$PHASE2_STATE_DIR" bash "$RESET"
COEFFS_BEFORE="$SESSION_DIR/fig7_qaccess_t_dynamic_coeffs_before.json"
cp "$RUNTIME_COEFFS" "$COEFFS_BEFORE"
start_worker

ACTIVE_DYNAMIC_TIMEOUT="$TIMEOUT"
if [[ "$EXECUTION_MODE" == "active" ]]; then
  ACTIVE_DYNAMIC_TIMEOUT=$((TIMEOUT + POST_UPDATE_OBSERVE_SEC))
  echo "[fig7_hybrid] active post-update observe window=${POST_UPDATE_OBSERVE_SEC}s dynamic_timeout=${ACTIVE_DYNAMIC_TIMEOUT}s"
fi
echo "[fig7_hybrid] dynamic leg: qaccess_t + delta_bw_1s worker ($EXECUTION_MODE, gate_mode=$GATE_MODE absolute=${GATE_BPS}bps relative=$MIN_RELATIVE_GAIN)"
echo "[fig7_hybrid] changed-path shadow: enabled=$CHANGED_PATH_PRIORITY_SHADOW ids=$CHANGED_PATH_IDS gain=${CHANGED_PATH_GAIN_BPS}bps aggregate>${MIN_AGGREGATE_GAIN_BPS} other_loss_ratio<=${MAX_OTHER_PATH_LOSS_RATIO} other_loss_bps<=${MAX_OTHER_PATH_LOSS_BPS}"
run_one qaccess_t fig7_qaccess_t_dynamic "$ACTIVE_DYNAMIC_TIMEOUT" \
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

if [[ -f "$TRIGGER_AUDIT_PATH" ]]; then
  cp "$TRIGGER_AUDIT_PATH" "$SESSION_DIR/qaccess_trigger_audit.jsonl"
fi
if [[ -f "$PHASE2_STATE_DIR/qaccess_owner_audit.jsonl" ]]; then
  cp "$PHASE2_STATE_DIR/qaccess_owner_audit.jsonl" "$SESSION_DIR/qaccess_owner_audit.jsonl"
fi

COEFFS_AFTER="$SESSION_DIR/fig7_qaccess_t_dynamic_coeffs_after.json"
cp "$RUNTIME_COEFFS" "$COEFFS_AFTER"
python3 - "$(worker_log_file)" "$SESSION_DIR/dynamic_coefficient_timeline.jsonl" <<'PY'
import json, sys
from pathlib import Path
source, target = map(Path, sys.argv[1:3])
rows = []
if source.is_file():
    for line in source.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append({key: row.get(key) for key in (
            "timestamp_ms", "request_id", "request_classification", "status", "gate_mode",
            "absolute_gain_bps", "relative_gain", "would_apply_under_gate", "actual_applied",
            "current_coefficients", "traffic_weighted_proposed_candidate",
            "traffic_weighted_proposed_stepped_coefficients", "applied_coefficients",
        )})
target.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
PY

SESSION_END="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - <<PY
import json, subprocess
from pathlib import Path

repo = Path("$ROOT")
session = repo / "$SESSION_DIR"
meta = {
    "session_id": session.name,
    "start_time": "$SESSION_START",
    "end_time": "$SESSION_END",
    "scenario": "$SCENARIO",
    "bw_profile": "$BW_PROFILE",
    "execution_mode": "$EXECUTION_MODE",
    "gate_mode": "$GATE_MODE",
    "min_relative_gain": float("$MIN_RELATIVE_GAIN"),
    "min_delta_gain_bps": float("$GATE_BPS"),
    "changed_path_priority_shadow": bool(int("$CHANGED_PATH_PRIORITY_SHADOW")),
    "changed_path_ids": [int(part) for part in "$CHANGED_PATH_IDS".split() if part.strip()],
    "changed_path_gain_bps": float("$CHANGED_PATH_GAIN_BPS"),
    "min_aggregate_gain_bps": float("$MIN_AGGREGATE_GAIN_BPS"),
    "max_other_path_loss_ratio": float("$MAX_OTHER_PATH_LOSS_RATIO"),
    "max_other_path_loss_bps": float("$MAX_OTHER_PATH_LOSS_BPS"),
    "timeout": int("$TIMEOUT"),
    "dynamic_timeout": int("$ACTIVE_DYNAMIC_TIMEOUT"),
    "post_update_observe_sec": int("$POST_UPDATE_OBSERVE_SEC"),
    "model_path": "$WORKER_MODEL",
    "model_metadata_path": "$WORKER_MODEL_METADATA",
}
try:
    meta["git_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    meta["branch"] = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True).strip()
except Exception:
    pass
(session / "experiment_metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
print("[fig7_hybrid] wrote", session / "experiment_metadata.json")
PY

echo ""
echo "[fig7_hybrid] session path: $ROOT/$SESSION_DIR"
echo "[fig7_hybrid] baseline run path: $SESSION_DIR/fig7_baseline"
echo "[fig7_hybrid] dynamic run path: $SESSION_DIR/fig7_qaccess_t_dynamic"
echo "[fig7_hybrid] worker log: $SESSION_DIR/worker.log"
echo "[fig7_hybrid] worker process log: $SESSION_DIR/worker_process.log"
echo "[fig7_hybrid] worker ready marker: $SESSION_DIR/worker_ready.json"
