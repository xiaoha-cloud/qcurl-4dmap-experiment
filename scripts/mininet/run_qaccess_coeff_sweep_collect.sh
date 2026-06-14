#!/usr/bin/env bash
# Collect Q-ACCeSS-T runtime samples under fixed alpha/beta/gamma settings.
#
# For each row in scripts/mininet/qaccess_coeff_sweep.csv:
#   - reset Phase 2 runtime state (does not touch initial coefficients)
#   - write sweep coefficients to derived/qaccess_t_runtime_coefficients.json
#   - run utility-mode qaccess_t with sample export (no worker / no coeff reload)
#   - save samples to derived/coeff_sweep/qaccess_samples_<name>.csv
#
# After all rows, merge and retrain (repo root, no sudo):
#   python3 scripts/analyze/merge_qaccess_coeff_sweep_samples.py
#   python3 scripts/analyze/preprocess_qaccess_training.py \
#     --input derived/qaccess_training_samples_coeff_sweep.csv \
#     --output derived/qaccess_training_samples_coeff_sweep_clean.csv
#   python3 scripts/analyze/train_qaccess_t.py \
#     --input derived/qaccess_training_samples_coeff_sweep_clean.csv \
#     --model-out derived/qaccess_t_model_coeff_sweep.pkl \
#     --metrics-out derived/qaccess_t_validation_metrics_coeff_sweep.json \
#     --importance-out derived/qaccess_t_feature_importance_coeff_sweep.csv \
#     --max-samples 0
#
# Usage (VM, repo root):
#   chmod +x scripts/mininet/run_qaccess_coeff_sweep_collect.sh
#   TIMEOUT=120 INPUT_FLV=~/Videos/push_input.flv \
#     sudo -E ./scripts/mininet/run_qaccess_coeff_sweep_collect.sh
#
# Env:
#   TIMEOUT          default 120 (debug); use 220+ for full Fig.7 profile
#   SAVE_LOGS        default 0
#   INPUT_FLV        optional push input FLV
#   SCENARIO         default fig7
#   DYNAMIC_PROFILE  default scripts/mininet/bw_profile.fig7_200s.env
#   PROFILE_TYPE     default bw (bw | deterioration)
#   LOG_CONTROL      default 0

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
SWEEP_CSV="$ROOT/scripts/mininet/qaccess_coeff_sweep.csv"
RUNTIME_COEFFS="$ROOT/derived/qaccess_t_runtime_coefficients.json"
OUT_DIR="$ROOT/derived/coeff_sweep"

TIMEOUT="${TIMEOUT:-120}"
SAVE_LOGS="${SAVE_LOGS:-0}"
INPUT_FLV="${INPUT_FLV:-}"
SCENARIO="${SCENARIO:-fig7}"
DYNAMIC_PROFILE="${DYNAMIC_PROFILE:-scripts/mininet/bw_profile.fig7_200s.env}"
PROFILE_TYPE="${PROFILE_TYPE:-bw}"
LOG_CONTROL="${LOG_CONTROL:-0}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

if [[ ! -f "$SWEEP_CSV" ]]; then
  echo "[error] missing sweep config: $SWEEP_CSV" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived logs_exp "$OUT_DIR"

SESSION_DIR="logs_exp/session_coeff_sweep_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

echo "[coeff_sweep] session=$SESSION_DIR"
echo "[coeff_sweep] scenario=$SCENARIO profile_type=$PROFILE_TYPE profile=$DYNAMIC_PROFILE timeout=${TIMEOUT}s"
echo "[coeff_sweep] output_dir=$OUT_DIR"
echo "[coeff_sweep] reading $SWEEP_CSV"

write_runtime_coeffs() {
  local name="$1"
  local alpha="$2"
  local beta="$3"
  local gamma="$4"
  python3 - "$RUNTIME_COEFFS" "$name" "$alpha" "$beta" "$gamma" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
name, alpha, beta, gamma = sys.argv[2:6]
payload = {
    "alpha": float(alpha),
    "beta": float(beta),
    "gamma": float(gamma),
    "source": f"coeff_sweep:{name}",
    "metric": "predicted_next_bw_bps",
}
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
tmp.replace(path)
print(f"[coeff_sweep] wrote runtime coefficients: {path}")
print(json.dumps(payload, indent=2))
PY
}

run_sweep_leg() {
  local name="$1"
  local samples_csv="derived/coeff_sweep/qaccess_samples_${name}.csv"
  local label="coeff_sweep_${name}"

  local -a cmd=(
    python3 "$MP" --run-exp
    --scenario "$SCENARIO"
    --utility-mode qaccess_t
    --timeout "$TIMEOUT"
    --log-parent "$SESSION_DIR"
    --run-label "$label"
  )

  case "$PROFILE_TYPE" in
    bw)
      cmd+=(--dynamic-bw-profile "$DYNAMIC_PROFILE")
      ;;
    deterioration)
      cmd+=(--dynamic-deterioration-profile "$DYNAMIC_PROFILE")
      ;;
    *)
      echo "[error] unsupported PROFILE_TYPE=$PROFILE_TYPE (use bw or deterioration)" >&2
      exit 1
      ;;
  esac

  [[ "$SAVE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)

  echo "[coeff_sweep] --- row: $name ---"
  echo "[coeff_sweep] samples_csv=$samples_csv"

  bash "$RESET"

  # reset copies initial -> runtime; overwrite with this sweep row.
  write_runtime_coeffs "$name" "$2" "$3" "$4"

  echo "[coeff_sweep] running qaccess_t (fixed coeffs, export only, no worker trigger)"
  env \
    QACCESS_COEFFS_JSON="derived/qaccess_t_runtime_coefficients.json" \
    QACCESS_COEFF_RELOAD=0 \
    QACCESS_TRIGGER_UPDATE=0 \
    QACCESS_RUNTIME_SAMPLE_EXPORT=1 \
    QACCESS_RUNTIME_SAMPLES_CSV="$samples_csv" \
    QACCESS_RUNTIME_BUFFER_SIZE=0 \
    QACCESS_LABEL_INTERVAL_MS="${QACCESS_LABEL_INTERVAL_MS:-100}" \
    "${cmd[@]}"

  if [[ -f "$ROOT/$samples_csv" ]]; then
    local nrows
    nrows=$(($(wc -l < "$ROOT/$samples_csv") - 1))
    echo "[coeff_sweep] saved $samples_csv rows=$nrows"
  else
    echo "[coeff_sweep] warning: missing $samples_csv after run" >&2
  fi
}

while IFS=$'\t' read -r name alpha beta gamma; do
  [[ -z "$name" ]] && continue
  run_sweep_leg "$name" "$alpha" "$beta" "$gamma"
done < <(
  python3 - "$SWEEP_CSV" <<'PY'
import csv
import sys

with open(sys.argv[1], newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        name = (row.get("name") or "").strip()
        if not name:
            continue
        print(
            f"{name}\t{row['alpha'].strip()}\t{row['beta'].strip()}\t{row['gamma'].strip()}"
        )
PY
)

echo ""
echo "[coeff_sweep] done. per-file samples:"
ls -lh "$OUT_DIR"/qaccess_samples_*.csv 2>/dev/null || true
echo ""
echo "[coeff_sweep] next (repo root, no sudo):"
echo "  python3 scripts/analyze/merge_qaccess_coeff_sweep_samples.py"
echo "  python3 scripts/analyze/build_qaccess_windowed_training.py --olia-only"
echo "  # low-RAM VM: add --per-sweep --sweep-dir derived/coeff_sweep"
echo "  python3 scripts/analyze/train_qaccess_t_grouped.py \\"
echo "    --input derived/qaccess_training_samples_coeff_sweep_olia_windowed.csv"
