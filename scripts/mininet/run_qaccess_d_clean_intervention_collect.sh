#!/usr/bin/env bash
# VM-only real intervention collector. Randomization changes assignment order, never measurements.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MANIFEST="$ROOT/scripts/mininet/qaccess_d_intervention_manifest_seed20260730.csv"
PROFILE="$ROOT/scripts/mininet/delay_profile.clean_40_80_40_200s.env"
STATE_DIR="${QACCESS_PHASE2_STATE_DIR:-$ROOT/derived/qaccess_d_intervention_collect_runtime}"
INPUT_FLV="${INPUT_FLV:-/home/mininet/Videos/push_input.flv}"
TIMEOUT="${TIMEOUT:-220}"
LIMIT=0; START_INDEX=1; RESUME=0; CHECK_ONLY=0; CONFIRM_FULL=0; SESSION_DIR=""

while (($#)); do
  case "$1" in
    --check-only) CHECK_ONLY=1 ;;
    --limit) LIMIT="${2:?missing value}"; shift ;;
    --start-index) START_INDEX="${2:?missing value}"; shift ;;
    --resume) RESUME=1 ;;
    --manifest) MANIFEST="${2:?missing value}"; shift ;;
    --session) SESSION_DIR="${2:?missing value}"; shift ;;
    --confirm-full) CONFIRM_FULL=1 ;;
    *) echo "[error] unsupported argument: $1" >&2; exit 2 ;;
  esac
  shift
done
for value in "$TIMEOUT" "$LIMIT" "$START_INDEX"; do
  [[ "$value" =~ ^[0-9]+$ ]] || { echo "[error] numeric arguments must be integers" >&2; exit 2; }
done
((TIMEOUT >= 110 && START_INDEX >= 1)) || { echo "[error] TIMEOUT>=110 and start-index>=1 required" >&2; exit 2; }
[[ "$STATE_DIR" = /* ]] || { echo "[error] state directory must be absolute" >&2; exit 2; }
for file in "$MANIFEST" "$PROFILE" "$ROOT/scripts/mininet/mp_topo.py" "$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh" "$ROOT/scripts/mininet/apply_qaccess_coefficients_at.py" "$ROOT/scripts/analyze/validate_qaccess_d_intervention_run.py"; do
  [[ -s "$file" ]] || { echo "[error] missing: $file" >&2; exit 1; }
done

manifest_rows="$(python3 - "$MANIFEST" <<'PY'
import csv, sys
with open(sys.argv[1], newline='', encoding='utf-8') as f: rows=list(csv.DictReader(f))
need={'run_order','candidate_id','replicate','alpha','beta','gamma','intervention_s','seed','is_sham'}
missing=need.difference(rows[0] if rows else {})
if missing: raise SystemExit('manifest missing: '+','.join(sorted(missing)))
print(len(rows))
PY
)"
echo "[check] repository_root=$ROOT"
echo "[check] manifest=$MANIFEST rows=$manifest_rows profile=$PROFILE"
echo "[check] production_data_source=VM_MININET_SENDER_RUNTIME_SAMPLES"
echo "[check] target=candidate_post_rtt_median_ms"
if ((CHECK_ONLY)); then echo "[check] valid; no experiment or production data created"; exit 0; fi
[[ "$(uname -s)" == Linux ]] || { echo "[error] real collection is VM/Linux only" >&2; exit 1; }
((EUID == 0)) || { echo "[error] run with sudo (Mininet needs root)" >&2; exit 1; }
[[ -s "$INPUT_FLV" ]] || { echo "[error] missing input: $INPUT_FLV" >&2; exit 1; }
[[ -x "$ROOT/4dmap" && -x "$ROOT/qserver" ]] || { echo "[error] rebuild 4dmap and qserver first" >&2; exit 1; }
selected=$((manifest_rows - START_INDEX + 1)); ((selected >= 0)) || selected=0
if ((LIMIT > 0 && LIMIT < selected)); then selected=$LIMIT; fi
if ((selected > 5 && CONFIRM_FULL == 0)); then
  echo "[error] $selected runs selected; use --limit 5 for smoke or add --confirm-full" >&2; exit 2
fi

cd "$ROOT"
[[ -n "$SESSION_DIR" ]] || SESSION_DIR="logs_exp/session_clean_d_intervention_$(date +%Y%m%d_%H%M%S)"
[[ "$SESSION_DIR" = /* ]] || SESSION_DIR="$ROOT/$SESSION_DIR"
mkdir -p "$SESSION_DIR" "$ROOT/logs_exp"
printf '%s\n' "${SESSION_DIR#"$ROOT/"}" > "$ROOT/logs_exp/.last_session"
cp "$MANIFEST" "$SESSION_DIR/intervention_manifest.csv"

run_row() {
  local order="$1" candidate="$2" replicate="$3" alpha="$4" beta="$5" gamma="$6" at="$7" seed="$8" sham="$9"
  local label
  label="$(printf 'd_intervention_%03d_%s_r%s' "$order" "$candidate" "$replicate")"
  local leg="$SESSION_DIR/$label" validation="$SESSION_DIR/$label/intervention_validation.json"
  if ((RESUME)) && [[ -s "$validation" ]] && python3 -c "import json; assert json.load(open('$validation'))['valid']" 2>/dev/null; then echo "[collect] skip validated row $order"; return; fi
  mkdir -p "$leg"
  QACCESS_PHASE2_STATE_DIR="$STATE_DIR" QACCESS_WORKER_TARGET_MODE=delta_owd_1s bash "$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
  local runtime="$STATE_DIR/qaccess_t_runtime_coefficients.json"
  python3 "$ROOT/scripts/mininet/apply_qaccess_coefficients_at.py" --log-dir "$leg/logs" \
    --coefficients "$runtime" --metadata-out "$leg/intervention_metadata.json" \
    --path-id "${QACCESS_INTERVENTION_PATH_ID:-3}" --intervention-s "$at" \
    --alpha "$alpha" --beta "$beta" --gamma "$gamma" --candidate-id "$candidate" \
    --replicate "$replicate" --run-order "$order" --seed "$seed" >"$leg/intervention_helper.log" 2>&1 &
  local helper=$!
  echo "[collect] row=$order candidate=$candidate replicate=$replicate intervention=$at seed=$seed sham=$sham"
  set +e
  env QACCESS_PHASE2_STATE_DIR="$STATE_DIR" QACCESS_COEFFS_JSON="$runtime" QACCESS_COEFF_RELOAD=1 \
    QACCESS_COEFF_RELOAD_INTERVAL_MS=250 QACCESS_COEFF_SMOOTHING=1 QACCESS_TRIGGER_UPDATE=0 QACCESS_RUNTIME_SAMPLE_EXPORT=1 \
    QACCESS_RUNTIME_SAMPLES_CSV="$STATE_DIR/qaccess_runtime_samples.csv" TC_DELAY_FIXED_BW_MBIT=20 \
    TC_DELAY_FIXED_LOSS_PERCENT=0 KEEP_PCAP="${KEEP_PCAP:-0}" SAVE_OUTPUT_FLV=0 \
    python3 "$ROOT/scripts/mininet/mp_topo.py" --run-exp --scenario clean_equal_paths --utility-mode qaccess_d \
      --timeout "$TIMEOUT" --log-parent "$SESSION_DIR" --run-label "$label" \
      --dynamic-delay-profile "$PROFILE" --input-flv "$INPUT_FLV" --log-control
  local run_status=$?; wait "$helper"; local helper_status=$?; set -e
  [[ -s "$STATE_DIR/qaccess_runtime_samples.csv" ]] && cp "$STATE_DIR/qaccess_runtime_samples.csv" "$leg/qaccess_runtime_samples.csv"
  ((run_status == 0 && helper_status == 0)) || { echo "[error] row $order failed: run=$run_status helper=$helper_status" >&2; return 1; }
  python3 "$ROOT/scripts/analyze/validate_qaccess_d_intervention_run.py" \
    --samples "$leg/qaccess_runtime_samples.csv" --intervention "$leg/intervention_metadata.json" --output "$validation"
}

while IFS=$'\t' read -r order candidate replicate alpha beta gamma at seed sham; do
  ((order >= START_INDEX)) || continue
  if ((LIMIT > 0 && order >= START_INDEX + LIMIT)); then break; fi
  run_row "$order" "$candidate" "$replicate" "$alpha" "$beta" "$gamma" "$at" "$seed" "$sham"
done < <(python3 - "$MANIFEST" <<'PY'
import csv, sys
with open(sys.argv[1], newline='', encoding='utf-8') as f:
 for r in csv.DictReader(f): print('\t'.join(r[k] for k in ('run_order','candidate_id','replicate','alpha','beta','gamma','intervention_s','seed','is_sham')))
PY
)
echo "[collect] complete: $SESSION_DIR"
