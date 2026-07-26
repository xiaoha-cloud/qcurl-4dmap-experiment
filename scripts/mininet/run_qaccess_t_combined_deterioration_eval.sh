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
CONFIGURATION_ONLY=0
case "${1:-}" in
  --check-only)
    CHECK_ONLY=1
    shift
    ;;
  --configuration-only)
    CONFIGURATION_ONLY=1
    shift
    ;;
esac
if [[ "$#" -ne 0 ]]; then
  echo "[error] unsupported argument: $1" >&2
  exit 2
fi
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
FINALIZE="$ROOT/scripts/mininet/finalize_experiment_leg.sh"
CLEAN_CONFIG="$ROOT/scripts/mininet/clean_experiment_config.py"
PHASE2_STATE_DIR="${QACCESS_PHASE2_STATE_DIR:-$ROOT/derived}"
SCENARIO="${SCENARIO:-fig8}"
CONTROLLER_VARIANT="${QACCESS_CONTROLLER_VARIANT:-qaccess_t}"
PROFILE_KIND="${QACCESS_PROFILE_KIND:-combined}"
if [[ "$PROFILE_KIND" == "none" ]]; then
  DETERIORATION_PROFILE="${DETERIORATION_PROFILE-}"
else
  DETERIORATION_PROFILE="${DETERIORATION_PROFILE:-scripts/mininet/combined_deterioration_profile_90_150.env}"
fi
EXPERIMENT_FAMILY="${QACCESS_EXPERIMENT_FAMILY:-historical}"
TIMEOUT="${TIMEOUT:-220}"
POST_UPDATE_OBSERVE_SEC="${QACCESS_POST_UPDATE_OBSERVE_SEC:-15}"
SAVE_VERBOSE_LOGS="${SAVE_VERBOSE_LOGS:-0}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"
BUFFER_SIZE="${QACCESS_RUNTIME_BUFFER_SIZE:-3000}"
MIN_SAMPLES_PER_PATH="${QACCESS_MIN_SAMPLES_PER_PATH:-1}"
MIN_SENDER_BYTE_DELTA="${QACCESS_MIN_SENDER_BYTE_DELTA:-1}"
COEFF_RELOAD_INTERVAL_MS="${QACCESS_COEFF_RELOAD_INTERVAL_MS:-1000}"
COEFF_SMOOTHING="${QACCESS_COEFF_SMOOTHING:-1}"
WORKER_PYTHON="${WORKER_PYTHON:-${REPO_ROOT}/.venv/bin/python3}"
WORKER_MODEL="${QACCESS_WORKER_MODEL:-derived/qaccess_t_redesign/qaccess_t_model_delta_bw_1s.pkl}"
WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-derived/qaccess_t_redesign/qaccess_t_redesign_report.json}"
WORKER_TARGET_MODE="${QACCESS_WORKER_TARGET_MODE:-${QACCESS_TARGET_MODE:-delta_bw_1s}}"
EXECUTION_MODE="${QACCESS_EXECUTION_MODE:-shadow}"
MULTIPATH_SHADOW_SCORING=1
COOLDOWN_MS="${QACCESS_TRIGGER_COOLDOWN_MS:-60000}"
GATE_BPS="${QACCESS_GATE_BPS:-${QACCESS_MIN_DELTA_GAIN_BPS:-500000}}"
GATE_MODE="${QACCESS_GATE_MODE:-absolute}"
MIN_RELATIVE_GAIN="${QACCESS_MIN_RELATIVE_GAIN:-0.03}"
MIN_OBJECTIVE_IMPROVEMENT="${QACCESS_MIN_OBJECTIVE_IMPROVEMENT:-}"
GATE_POLICY="${QACCESS_GATE_POLICY:-legacy}"
TRIGGER_MODE="${QACCESS_TRIGGER_MODE:-legacy_buffer_full}"
case "$CONTROLLER_VARIANT" in
  qaccess_t) DEFAULT_GATE_OBJECTIVE=throughput ;;
  qaccess_d) DEFAULT_GATE_OBJECTIVE=delay ;;
  qaccess_l) DEFAULT_GATE_OBJECTIVE=loss ;;
esac
GATE_OBJECTIVE="${QACCESS_GATE_OBJECTIVE:-$DEFAULT_GATE_OBJECTIVE}"
case "$GATE_OBJECTIVE" in
  throughput) DEFAULT_OBJECTIVE_RELATIVE_IMPROVEMENT=0.05 ;;
  delay) DEFAULT_OBJECTIVE_RELATIVE_IMPROVEMENT=0.10 ;;
  loss) DEFAULT_OBJECTIVE_RELATIVE_IMPROVEMENT=0.25 ;;
esac
MIN_OBJECTIVE_RELATIVE_IMPROVEMENT="${QACCESS_MIN_OBJECTIVE_RELATIVE_IMPROVEMENT:-$DEFAULT_OBJECTIVE_RELATIVE_IMPROVEMENT}"
if [[ -z "$MIN_OBJECTIVE_IMPROVEMENT" ]]; then
  if [[ "$GATE_POLICY" == "objective_aware" ]]; then
    case "$GATE_OBJECTIVE" in
      delay) MIN_OBJECTIVE_IMPROVEMENT=10 ;;
      loss) MIN_OBJECTIVE_IMPROVEMENT=4096 ;;
      *) MIN_OBJECTIVE_IMPROVEMENT=0 ;;
    esac
  else
    MIN_OBJECTIVE_IMPROVEMENT=0
  fi
fi
SESSION_KIND="${QACCESS_SESSION_KIND:-combined_deterioration}"
BASELINE_LABEL="${QACCESS_BASELINE_LABEL:-combined_baseline}"
DYNAMIC_LABEL="${QACCESS_DYNAMIC_LABEL:-combined_${CONTROLLER_VARIANT}_dynamic}"

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
if [[ -n "$DETERIORATION_PROFILE" ]]; then
  DETERIORATION_PROFILE="$(resolve_repo_path "$DETERIORATION_PROFILE")"
fi
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
case "$GATE_POLICY" in
  legacy|objective_aware) ;;
  *) echo "[error] QACCESS_GATE_POLICY must be legacy or objective_aware, got: $GATE_POLICY" >&2; exit 2 ;;
esac
case "$TRIGGER_MODE:$GATE_OBJECTIVE" in
  legacy_buffer_full:throughput|legacy_buffer_full:delay|legacy_buffer_full:loss|objective_t:throughput|objective_d:delay|objective_l:loss) ;;
  *) echo "[error] incompatible trigger/objective: $TRIGGER_MODE / $GATE_OBJECTIVE" >&2; exit 2 ;;
esac
case "$CONTROLLER_VARIANT:$GATE_OBJECTIVE" in
  qaccess_t:throughput|qaccess_d:delay|qaccess_l:loss) ;;
  *) echo "[error] incompatible controller/objective: $CONTROLLER_VARIANT / $GATE_OBJECTIVE" >&2; exit 2 ;;
esac
if [[ "$GATE_POLICY" == "objective_aware" && "$TRIGGER_MODE" == "legacy_buffer_full" ]]; then
  echo "[error] objective_aware gate policy requires an objective-specific trigger" >&2
  exit 2
fi
case "$CONTROLLER_VARIANT:$WORKER_TARGET_MODE" in
  qaccess_t:delta_bw_1s|qaccess_d:delta_owd_1s|qaccess_l:delta_loss_1s|qaccess_l:loss_risk_1s) ;;
  *)
  echo "[error] incompatible controller/target: $CONTROLLER_VARIANT / $WORKER_TARGET_MODE" >&2
  exit 2
  ;;
esac
case "$PROFILE_KIND" in
  combined|bandwidth|delay|loss|none) ;;
  *) echo "[error] QACCESS_PROFILE_KIND must be combined, bandwidth, delay, loss, or none" >&2; exit 2 ;;
esac
if [[ "$EXPERIMENT_FAMILY" == "clean_controlled" ]]; then
  [[ "$GATE_POLICY" == "objective_aware" ]] || { echo "[error] clean runners require QACCESS_GATE_POLICY=objective_aware" >&2; exit 2; }
  [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || { echo "[error] clean experiment TIMEOUT must be an integer number of seconds" >&2; exit 2; }
  ((TIMEOUT >= 200)) || { echo "[error] clean experiment TIMEOUT must be at least 200 seconds" >&2; exit 2; }
  if [[ "$PROFILE_KIND" == "bandwidth" ]]; then
    [[ "${TC_BW_FIXED_DELAY_MS:-}" == "40" ]] || { echo "[error] clean bandwidth requires TC_BW_FIXED_DELAY_MS=40" >&2; exit 2; }
    [[ "${TC_BW_FIXED_LOSS_PERCENT:-}" == "0" ]] || { echo "[error] clean bandwidth requires TC_BW_FIXED_LOSS_PERCENT=0" >&2; exit 2; }
  elif [[ "$PROFILE_KIND" == "delay" ]]; then
    [[ "${TC_DELAY_FIXED_BW_MBIT:-}" == "20" ]] || { echo "[error] clean delay requires TC_DELAY_FIXED_BW_MBIT=20" >&2; exit 2; }
    [[ "${TC_DELAY_FIXED_LOSS_PERCENT:-}" == "0" ]] || { echo "[error] clean delay requires TC_DELAY_FIXED_LOSS_PERCENT=0" >&2; exit 2; }
  fi
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
  --controller-variant "$CONTROLLER_VARIANT"
  --request "$PHASE2_STATE_DIR/qaccess_update_request.json"
  --runtime-samples "$PHASE2_STATE_DIR/qaccess_runtime_samples.csv"
  --coeffs-out "$PHASE2_STATE_DIR/qaccess_t_runtime_coefficients.json"
  --response-out "$PHASE2_STATE_DIR/qaccess_update_response.json"
  --state "$PHASE2_STATE_DIR/qaccess_worker_state.json"
  --archive-dir "$PHASE2_STATE_DIR/qaccess_processed_buffers"
  --audit-csv "$PHASE2_STATE_DIR/qaccess_update_audit.csv"
  --min-delta-gain-bps "$GATE_BPS"
  --min-objective-improvement "$MIN_OBJECTIVE_IMPROVEMENT"
  --gate-mode "$GATE_MODE"
  --gate-policy "$GATE_POLICY"
  --objective "$GATE_OBJECTIVE"
  --min-relative-gain "$MIN_RELATIVE_GAIN"
  --min-objective-relative-improvement "$MIN_OBJECTIVE_RELATIVE_IMPROVEMENT"
  --min-sender-byte-delta "$MIN_SENDER_BYTE_DELTA"
  --aggregate-multipath
)
if [[ "$EXECUTION_MODE" == "shadow" ]]; then
  WORKER_CMD+=(--shadow-per-subflow)
fi
WORKER_PID=""
WORKER_READY_TIMEOUT="${QACCESS_WORKER_READY_TIMEOUT:-30}"

validate_profile() {
  if [[ "$EXPERIMENT_FAMILY" == "clean_controlled" ]]; then
    local -a config_cmd=(
      python3 "$CLEAN_CONFIG"
      --scenario "$SCENARIO"
      --profile-kind "$PROFILE_KIND"
    )
    if [[ "$PROFILE_KIND" != "none" ]]; then
      config_cmd+=(--profile "$DETERIORATION_PROFILE")
    fi
    "${config_cmd[@]}"
    return
  fi
  [[ -f "$DETERIORATION_PROFILE" ]] || { echo "[error] missing deterioration profile: $DETERIORATION_PROFILE" >&2; return 1; }
  grep -q '^IFACE=h2-eth1$' "$DETERIORATION_PROFILE" || {
    echo "[error] profile must target h2-eth1: $DETERIORATION_PROFILE" >&2
    return 1
  }
}

check_configuration() {
  echo "[check] repository_root=$ROOT"
  echo "[check] experiment_family=$EXPERIMENT_FAMILY"
  echo "[check] scenario=$SCENARIO"
  echo "[check] input_media=${INPUT_FLV:-<mp_topo-default>}"
  if [[ "$EXPERIMENT_FAMILY" != "clean_controlled" && ( -z "$INPUT_FLV" || ! -f "$INPUT_FLV" ) ]]; then
    echo "[FAIL] input media is missing: ${INPUT_FLV:-<unset>}" >&2
    return 1
  fi
  if [[ "$EXPERIMENT_FAMILY" == "clean_controlled" && -n "$INPUT_FLV" && ! -f "$INPUT_FLV" ]]; then
    echo "[FAIL] input media is missing: $INPUT_FLV" >&2
    return 1
  fi
  echo "[check] model_path=$WORKER_MODEL"
  [[ -f "$WORKER_MODEL" ]] || { echo "[FAIL] model is missing: $WORKER_MODEL" >&2; return 1; }
  echo "[check] model_metadata=$WORKER_MODEL_METADATA"
  [[ -f "$WORKER_MODEL_METADATA" ]] || { echo "[FAIL] model metadata is missing: $WORKER_MODEL_METADATA" >&2; return 1; }
  echo "[check] model_exists=true"
  echo "[check] requested_target_mode=$WORKER_TARGET_MODE"
  echo "[check] controller_variant=$CONTROLLER_VARIANT"
  echo "[check] profile_kind=$PROFILE_KIND"
  echo "[check] deterioration_profile=${DETERIORATION_PROFILE:-none}"
  validate_profile
  echo "[check] execution_mode=$EXECUTION_MODE"
  echo "[check] buffer=$BUFFER_SIZE"
  echo "[check] cooldown_ms=$COOLDOWN_MS"
  echo "[check] gate_bps=$GATE_BPS"
  echo "[check] min_objective_improvement=$MIN_OBJECTIVE_IMPROVEMENT"
  echo "[check] gate_mode=$GATE_MODE min_relative_gain=$MIN_RELATIVE_GAIN"
  echo "[check] gate_policy=$GATE_POLICY objective=$GATE_OBJECTIVE trigger_mode=$TRIGGER_MODE"
  [[ -x "$WORKER_PYTHON" ]] || { echo "[FAIL] worker Python is not executable: $WORKER_PYTHON" >&2; return 1; }
  "$WORKER_PYTHON" scripts/analyze/qaccess_t_update_worker.py \
    --model "$WORKER_MODEL" \
    --model-metadata "$WORKER_MODEL_METADATA" \
    --target-mode "$WORKER_TARGET_MODE" \
    --controller-variant "$CONTROLLER_VARIANT" \
    --validate-model-only
  echo "[PASS] configuration is compatible; no Mininet, TC or runtime state was started"
}

check_clean_configuration_only() {
  [[ "$EXPERIMENT_FAMILY" == "clean_controlled" ]] || {
    echo "[FAIL] --configuration-only is limited to clean controlled runners" >&2
    return 1
  }
  echo "[configuration] repository_root=$ROOT"
  echo "[configuration] experiment_family=$EXPERIMENT_FAMILY"
  echo "[configuration] scenario=$SCENARIO"
  echo "[configuration] input_media=${INPUT_FLV:-<mp_topo-default>}"
  echo "[configuration] model_target=$WORKER_TARGET_MODE"
  echo "[configuration] model_path=$WORKER_MODEL"
  echo "[configuration] model_metadata=$WORKER_MODEL_METADATA"
  echo "[configuration] profile_kind=$PROFILE_KIND"
  echo "[configuration] profile=${DETERIORATION_PROFILE:-none}"
  echo "[configuration] duration_sec=$TIMEOUT"
  echo "[configuration] execution_mode=$EXECUTION_MODE"
  if [[ "$PROFILE_KIND" == "bandwidth" ]]; then
    echo "[configuration] qdisc_hierarchy=root_tbf_1_to_child_netem_10 fixed_delay_ms=$TC_BW_FIXED_DELAY_MS fixed_loss_percent=$TC_BW_FIXED_LOSS_PERCENT"
  elif [[ "$PROFILE_KIND" == "delay" ]]; then
    echo "[configuration] qdisc_hierarchy=root_tbf_1_to_child_netem_10 fixed_bw_mbit=$TC_DELAY_FIXED_BW_MBIT fixed_loss_percent=$TC_DELAY_FIXED_LOSS_PERCENT"
  fi
  validate_profile
  echo "[configuration] gate_policy=$GATE_POLICY objective=$GATE_OBJECTIVE trigger_mode=$TRIGGER_MODE gate_mode=$GATE_MODE min_delta_gain_bps=$GATE_BPS min_relative_gain=$MIN_RELATIVE_GAIN min_objective_improvement=$MIN_OBJECTIVE_IMPROVEMENT min_objective_relative_improvement=$MIN_OBJECTIVE_RELATIVE_IMPROVEMENT"
  echo "[PASS] clean runner configuration is valid; production model validation was not requested"
}

if [[ "$CONFIGURATION_ONLY" == "1" ]]; then
  cd "$ROOT"
  check_clean_configuration_only
  exit $?
fi

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

SESSION_DIR="logs_exp/session_${SESSION_KIND}_$(date +%Y%m%d_%H%M%S)"
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

STATE_LOCK_DIR=""

release_state_lock() {
  if [[ -n "$STATE_LOCK_DIR" ]]; then
    rmdir "$STATE_LOCK_DIR" 2>/dev/null || true
    STATE_LOCK_DIR=""
  fi
}

if [[ "$EXPERIMENT_FAMILY" == "clean_controlled" ]]; then
  mkdir -p "$PHASE2_STATE_DIR"
  STATE_LOCK_DIR="$PHASE2_STATE_DIR/.clean_experiment_lock"
  if ! mkdir "$STATE_LOCK_DIR" 2>/dev/null; then
    echo "[error] Phase 2 state directory is already in use: $PHASE2_STATE_DIR" >&2
    exit 1
  fi
fi

cleanup() {
  stop_worker
  release_state_lock
}

trap cleanup EXIT INT TERM

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
  local preflight_scope="${2:-runtime}"
  echo "[combined_deterioration] worker python: $WORKER_PYTHON"
  if [[ ! -x "$WORKER_PYTHON" ]]; then
    echo "[error] WORKER_PYTHON is not executable: $WORKER_PYTHON" >&2
    return 1
  fi
  "$WORKER_PYTHON" --version 2>&1 | sed 's/^/[combined_deterioration] worker python version: /'
  "$WORKER_PYTHON" - "$ROOT" "$WORKER_MODEL" "$WORKER_MODEL_METADATA" "$PHASE2_STATE_DIR" "$preflight_scope" >"$process_log" 2>&1 <<'PY'
import sys
from pathlib import Path

repo = Path(sys.argv[1])
model_path = Path(sys.argv[2])
metadata_path = Path(sys.argv[3])
state_dir = Path(sys.argv[4])
preflight_scope = sys.argv[5]
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

if preflight_scope == "runtime":
    if not state_dir.is_absolute():
        raise ValueError(f"Phase 2 state dir must be absolute: {state_dir}")
    coeffs_path = state_dir / "qaccess_t_runtime_coefficients.json"
    if not coeffs_path.is_file():
        raise FileNotFoundError(f"missing runtime coefficients {coeffs_path}")
    with coeffs_path.open("a", encoding="utf-8"):
        pass
    print(f"[worker-preflight] coeffs_writable=ok path={coeffs_path}", flush=True)
elif preflight_scope == "model_only":
    print("[worker-preflight] runtime_coefficients=deferred_until_post_reset", flush=True)
else:
    raise ValueError(f"unsupported preflight scope: {preflight_scope}")
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
    --controller-variant "$CONTROLLER_VARIANT" \
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
    default = c.get('default') or {}
    alpha = c.get('alpha', default.get('alpha'))
    beta = c.get('beta', default.get('beta'))
    gamma = c.get('gamma', default.get('gamma'))
    paths = c.get('paths') or {}
    suffix = ''
    if paths:
        rendered = ','.join('{}={}/{}/{}'.format(k, v.get('alpha'), v.get('beta'), v.get('gamma')) for k, v in sorted(paths.items()))
        suffix = ' paths=[' + rendered + ']'
    print('alpha={} beta={} gamma={} source={} metric={} target_mode={}{}'.format(
        alpha, beta, gamma, c.get('source', ''), c.get('metric', ''), c.get('target_mode', ''), suffix))
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
  local run_timeout="$3"
  shift 3
  local -a cmd=(
    python3 "$MP" --run-exp --scenario "$SCENARIO" --utility-mode "$um"
    --timeout "$run_timeout" --log-parent "$SESSION_DIR" --run-label "$label"
  )
  case "$PROFILE_KIND" in
    combined) cmd+=(--dynamic-deterioration-profile "$DETERIORATION_PROFILE") ;;
    bandwidth) cmd+=(--dynamic-bw-profile "$DETERIORATION_PROFILE") ;;
    delay) cmd+=(--dynamic-delay-profile "$DETERIORATION_PROFILE") ;;
    loss) cmd+=(--dynamic-loss-profile "$DETERIORATION_PROFILE") ;;
    none) ;;
  esac
  [[ "$SAVE_VERBOSE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[combined_deterioration] scenario=$SCENARIO utility-mode=$um label=$label profile=${DETERIORATION_PROFILE:-none}"
  echo "[combined_deterioration] KEEP_PCAP=$KEEP_PCAP SAVE_OUTPUT_FLV=$SAVE_OUTPUT_FLV KEEP_RAW_RUNTIME=$KEEP_RAW_RUNTIME SAVE_VERBOSE_LOGS=$SAVE_VERBOSE_LOGS"
  env "$@" "${cmd[@]}"
  finalize_leg "$label" || true
}

echo "[combined_deterioration] profile contents:"
validate_profile
if [[ "$PROFILE_KIND" != "none" ]]; then
  cat "$DETERIORATION_PROFILE"
else
  echo "none"
fi
echo ""
echo "[combined_deterioration] experiment_family=$EXPERIMENT_FAMILY scenario=$SCENARIO"
echo "[combined_deterioration] model_target=$WORKER_TARGET_MODE model=$WORKER_MODEL metadata=$WORKER_MODEL_METADATA"
echo "[combined_deterioration] gate_policy=$GATE_POLICY objective=$GATE_OBJECTIVE trigger_mode=$TRIGGER_MODE gate_mode=$GATE_MODE min_delta_gain_bps=$GATE_BPS min_relative_gain=$MIN_RELATIVE_GAIN min_objective_improvement=$MIN_OBJECTIVE_IMPROVEMENT min_objective_relative_improvement=$MIN_OBJECTIVE_RELATIVE_IMPROVEMENT"

echo "[combined_deterioration] validating worker model before starting Mininet"
echo "[combined_deterioration] resolved worker model: $WORKER_MODEL"
echo "[combined_deterioration] requested target mode: $WORKER_TARGET_MODE"
preflight_worker_python "$(worker_process_log_file)" model_only

echo "[combined_deterioration] baseline leg (same deterioration profile, no qaccess, no worker)"
QACCESS_PHASE2_STATE_DIR="$PHASE2_STATE_DIR" bash "$RESET"
run_one baseline "$BASELINE_LABEL" "$TIMEOUT"

echo "[combined_deterioration] reset runtime + initialize coefficients from initial"
QACCESS_PHASE2_STATE_DIR="$PHASE2_STATE_DIR" bash "$RESET"
COEFFS_BEFORE="$SESSION_DIR/${DYNAMIC_LABEL}_coeffs_before.json"
cp "$RUNTIME_COEFFS" "$COEFFS_BEFORE"
echo "[combined_deterioration] runtime coefficients BEFORE dynamic leg:"
cat "$COEFFS_BEFORE"
echo "[combined_deterioration] parsed: $(read_coeffs "$COEFFS_BEFORE")"

start_worker

if [[ "$EXECUTION_MODE" == "active" ]]; then
  echo "[combined_deterioration] active aggregate safety checks enabled; traffic-weighted aggregate controls updates"
fi
echo "[combined_deterioration] dynamic leg: $CONTROLLER_VARIANT + $WORKER_TARGET_MODE worker ($EXECUTION_MODE, gate_mode=$GATE_MODE objective_threshold=$MIN_OBJECTIVE_IMPROVEMENT)"
echo "[combined_deterioration] worker model=$WORKER_MODEL metadata=$WORKER_MODEL_METADATA"
echo "[combined_deterioration] global buffer capacity=$BUFFER_SIZE min samples per path=$MIN_SAMPLES_PER_PATH"
ACTIVE_DYNAMIC_TIMEOUT="$TIMEOUT"
if [[ "$EXECUTION_MODE" == "active" && "$EXPERIMENT_FAMILY" != "clean_controlled" ]]; then
  ACTIVE_DYNAMIC_TIMEOUT=$((TIMEOUT + POST_UPDATE_OBSERVE_SEC))
  echo "[combined_deterioration] active post-update observe window=${POST_UPDATE_OBSERVE_SEC}s dynamic_timeout=${ACTIVE_DYNAMIC_TIMEOUT}s"
fi
run_one "$CONTROLLER_VARIANT" "$DYNAMIC_LABEL" "$ACTIVE_DYNAMIC_TIMEOUT" \
  QACCESS_PHASE2_STATE_DIR="$PHASE2_STATE_DIR" \
  QACCESS_COEFFS_JSON="$RUNTIME_COEFFS" \
  QACCESS_COEFF_RELOAD=1 \
  QACCESS_COEFF_RELOAD_INTERVAL_MS="$COEFF_RELOAD_INTERVAL_MS" \
  QACCESS_COEFF_SMOOTHING="$COEFF_SMOOTHING" \
  QACCESS_TRIGGER_UPDATE=1 \
  QACCESS_TRIGGER_MODE="$TRIGGER_MODE" \
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
            "absolute_gain_bps", "objective_gain_ms", "objective_gain_bytes", "relative_gain",
            "would_apply_under_gate", "actual_applied",
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

COEFFS_AFTER="$SESSION_DIR/${DYNAMIC_LABEL}_coeffs_after.json"
cp "$RUNTIME_COEFFS" "$COEFFS_AFTER"
echo ""
echo "[combined_deterioration] runtime coefficients AFTER dynamic leg:"
cat "$COEFFS_AFTER"
echo "[combined_deterioration] parsed: $(read_coeffs "$COEFFS_AFTER")"

CHANGED=$(python3 -c "
import json
b=json.load(open('$COEFFS_BEFORE'))
a=json.load(open('$COEFFS_AFTER'))
def norm(doc):
    keep = {}
    default = doc.get('default') or {}
    keep['default'] = {
        'alpha': doc.get('alpha', default.get('alpha')),
        'beta': doc.get('beta', default.get('beta')),
        'gamma': doc.get('gamma', default.get('gamma')),
    }
    keep['paths'] = {
        str(k): {'alpha': v.get('alpha'), 'beta': v.get('beta'), 'gamma': v.get('gamma')}
        for k, v in sorted((doc.get('paths') or {}).items())
    }
    return keep
print('yes' if norm(b) != norm(a) else 'no')
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
    'controller_variant': '$CONTROLLER_VARIANT',
    'objective': {
        'qaccess_t': 'throughput_aware',
        'qaccess_d': 'delay_aware',
        'qaccess_l': 'loss_aware',
    }.get('$CONTROLLER_VARIANT', 'unknown'),
    'profile_kind': '$PROFILE_KIND',
    'target_semantics': {
        'delta_bw_1s': 'per_path_future_bw_1s_minus_current_bw',
        'delta_owd_1s': 'per_path_future_owd_1s_minus_current_owd',
        'delta_loss_1s': 'per_path_future_loss_1s_minus_current_loss',
        'loss_risk_1s': 'per_path_sum_lost_retrans_bytes_within_next_1s',
    }.get('$WORKER_TARGET_MODE', ''),
    'gate_mode': '$GATE_MODE',
    'gate_policy': '$GATE_POLICY',
    'gate_objective': '$GATE_OBJECTIVE',
    'trigger_mode': '$TRIGGER_MODE',
    'min_delta_gain_bps': float('$GATE_BPS'),
    'min_objective_improvement': float('$MIN_OBJECTIVE_IMPROVEMENT'),
    'min_objective_relative_improvement': float('$MIN_OBJECTIVE_RELATIVE_IMPROVEMENT'),
    'min_relative_gain': float('$MIN_RELATIVE_GAIN'),
    'execution_mode': '$EXECUTION_MODE',
    'worker_shadow': '$EXECUTION_MODE' == 'shadow',
    'initial_coefficients': load('$COEFFS_BEFORE'),
    'final_coefficients': load('$COEFFS_AFTER'),
    'profile_path': '$DETERIORATION_PROFILE',
    'timeout': int('$TIMEOUT'),
    'dynamic_timeout': int('$ACTIVE_DYNAMIC_TIMEOUT'),
    'post_update_observe_sec': int('$POST_UPDATE_OBSERVE_SEC'),
    'KEEP_PCAP': int('$KEEP_PCAP'),
    'KEEP_RAW_RUNTIME': int('$KEEP_RAW_RUNTIME'),
    'SAVE_OUTPUT_FLV': int('$SAVE_OUTPUT_FLV'),
    'legs': {},
}
if '$EXPERIMENT_FAMILY' == 'clean_controlled':
    command = [
        __import__('sys').executable,
        str(repo / 'scripts/mininet/clean_experiment_config.py'),
        '--scenario', '$SCENARIO',
        '--profile-kind', '$PROFILE_KIND',
        '--json',
    ]
    if '$PROFILE_KIND' != 'none':
        command.extend(['--profile', '$DETERIORATION_PROFILE'])
    clean_config = json.loads(subprocess.check_output(command, cwd=repo, text=True))
    common_leg_config = {
        'scenario': clean_config['scenario'],
        'profile_path': clean_config['profile_path'],
        'transitions_sec': clean_config['transitions_sec'],
        'input_video': '$INPUT_FLV' or '<mp_topo-default>',
        'duration_sec': int('$TIMEOUT'),
        'impairment_interface': clean_config['dynamic_interface'],
    }
    baseline_leg_config = dict(common_leg_config)
    active_leg_config = dict(common_leg_config)
    meta.update(clean_config)
    meta.update({
        'objective': {
            'qaccess_t': 'throughput',
            'qaccess_d': 'delay',
            'qaccess_l': 'loss',
        }.get('$CONTROLLER_VARIANT', 'unknown'),
        'model_target': '$WORKER_TARGET_MODE',
        'gate_policy': '$GATE_POLICY',
        'trigger_mode': '$TRIGGER_MODE',
        'baseline_label': '$BASELINE_LABEL',
        'active_label': '$DYNAMIC_LABEL',
        'baseline_leg_configuration': baseline_leg_config,
        'active_leg_configuration': active_leg_config,
        'bandwidth_qdisc': ({
            'hierarchy': 'root_tbf_handle_1_child_netem_handle_10_parent_1_1',
            'root_command': 'tc qdisc replace dev h2-eth1 root handle 1: tbf rate <profile_mbps>mbit burst 64kbit latency 400ms',
            'child_command': 'tc qdisc replace dev h2-eth1 parent 1:1 handle 10: netem delay 40ms loss 0%',
            'fixed_delay_ms': 40,
            'fixed_loss_percent': 0,
        } if '$PROFILE_KIND' == 'bandwidth' else None),
        'paired_leg_validation': {
            'same_scenario': baseline_leg_config['scenario'] == active_leg_config['scenario'],
            'same_profile': baseline_leg_config['profile_path'] == active_leg_config['profile_path'],
            'same_transitions': baseline_leg_config['transitions_sec'] == active_leg_config['transitions_sec'],
            'same_input_video': baseline_leg_config['input_video'] == active_leg_config['input_video'],
            'same_duration': baseline_leg_config['duration_sec'] == int('$ACTIVE_DYNAMIC_TIMEOUT'),
            'same_impairment_interface': baseline_leg_config['impairment_interface'] == active_leg_config['impairment_interface'],
        },
    })
for leg_name in ('$BASELINE_LABEL', '$DYNAMIC_LABEL'):
    status_path = session / leg_name / 'leg_status.json'
    if status_path.is_file():
        meta['legs'][leg_name] = json.loads(status_path.read_text())
(session / 'experiment_metadata.json').write_text(json.dumps(meta, indent=2))
print('[combined_deterioration] wrote', session / 'experiment_metadata.json')
"

echo ""
echo "[combined_deterioration] session: $ROOT/$SESSION_DIR"
echo "[combined_deterioration] baseline:  $SESSION_DIR/$BASELINE_LABEL"
echo "[combined_deterioration] dynamic:   $SESSION_DIR/$DYNAMIC_LABEL"
echo "[combined_deterioration] worker log: $SESSION_DIR/worker.log"
echo "[combined_deterioration] worker process log: $SESSION_DIR/worker_process.log"
echo "[combined_deterioration] worker ready marker: $SESSION_DIR/worker_ready.json"
echo "[combined_deterioration] retained per leg: control_law_diagnostics.csv throughput_*_down.csv tc_deterioration.log (+ dynamic coeffs JSON at session root)"
if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
  chown -R "${SUDO_UID}:${SUDO_GID}" "$SESSION_DIR" 2>/dev/null \
    || echo "[warn] could not restore session ownership: $SESSION_DIR" >&2
fi
