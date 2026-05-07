#!/usr/bin/env bash
# Post-process flat logs_exp/vm_run_* directories into a batch folder with
# phase1/ and phase2/{baseline,delay,loss}/ subfolders (copy, move, or symlink).
#
# Classification:
#   - Contains tc_delay_*.log  → phase2/delay
#   - Contains tc_loss_*.log   → phase2/loss
#   - Exactly 3 logs (server, pull, push), no tc → "static" runs
#
# Baseline (Phase 2, no tc) is picked as the static run whose vm_run_* name sorts
# just before the first delay directory (same ordering as run_experiment_matrix.sh).
# Remaining static dirs (before that baseline) go to phase1/ as run_001, run_002, ...
#
# Usage (repo root):
#   chmod +x scripts/mininet/organize_logs.sh
#   ./scripts/mininet/organize_logs.sh [--root DIR] [--batch NAME] [--mode mv|cp|ln]
#
# Example:
#   ./scripts/mininet/organize_logs.sh --root logs_exp --batch 20260402_rerun --mode ln
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_ROOT="$ROOT/logs_exp"
BATCH_NAME=""
MODE="ln"

abspath_dir() {
  (cd "$1" && pwd)
}

log() { echo "[organize_logs] $*" >&2; }

usage() {
  sed -n '1,20p' "$0" | tail -n +2
  echo "Options: --root DIR   (default: repo/logs_exp)"
  echo "         --batch NAME (default: batch_\$(date +%Y%m%d_%H%M%S))"
  echo "         --mode mv|cp|ln  (default: ln)"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)
      if [[ "$2" == /* ]]; then LOG_ROOT="$(cd "$2" && pwd)"
      else LOG_ROOT="$(cd "$ROOT/$2" && pwd)"; fi
      shift 2 ;;
    --batch) BATCH_NAME="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) log "unknown arg: $1"; usage; exit 1 ;;
  esac
done

[[ -d "$LOG_ROOT" ]] || { log "not a directory: $LOG_ROOT"; exit 1; }
case "$MODE" in mv|cp|ln) ;; *) log "bad --mode $MODE"; exit 1 ;; esac

if [[ -z "$BATCH_NAME" ]]; then
  BATCH_NAME="batch_$(date +%Y%m%d_%H%M%S)"
fi

DEST="$LOG_ROOT/$BATCH_NAME"
if [[ -e "$DEST" ]]; then
  log "refusing to clobber existing $DEST"
  exit 1
fi

mapfile -t ALL_RUNS < <(find "$LOG_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'vm_run_*' | LC_ALL=C sort)

if [[ ${#ALL_RUNS[@]} -eq 0 ]]; then
  log "no vm_run_* directories under $LOG_ROOT"
  exit 0
fi

delay_dir=""
loss_dir=""
declare -a static_dirs=()

for d in "${ALL_RUNS[@]}"; do
  base="$(basename "$d")"
  shopt -s nullglob
  td=( "$d"/tc_delay_*.log )
  tl=( "$d"/tc_loss_*.log )
  shopt -u nullglob
  if [[ ${#td[@]} -gt 0 ]]; then
    if [[ -n "$delay_dir" ]]; then
      log "warning: multiple delay dirs; using first $delay_dir, also saw $d"
    else
      delay_dir="$d"
    fi
  elif [[ ${#tl[@]} -gt 0 ]]; then
    if [[ -n "$loss_dir" ]]; then
      log "warning: multiple loss dirs; using first $loss_dir, also saw $d"
    else
      loss_dir="$d"
    fi
  else
    # static (expect 3 main logs)
    static_dirs+=("$d")
  fi
done

baseline_dir=""
if [[ -n "$delay_dir" ]]; then
  # last static dir that sorts before delay_dir → baseline
  for s in "${static_dirs[@]}"; do
    if [[ "$s" < "$delay_dir" ]]; then
      baseline_dir="$s"
    fi
  done
fi

mkdir -p "$DEST/phase1" "$DEST/phase2/baseline" "$DEST/phase2/delay" "$DEST/phase2/loss"

do_link() {
  local src="$1" dst="$2"
  case "$MODE" in
    mv) mv "$src" "$dst" ;;
    cp) cp -a "$src" "$dst" ;;
    ln) ln -s "$(abspath_dir "$src")" "$dst" ;;
  esac
}

idx=1
for s in "${static_dirs[@]}"; do
  if [[ -n "$baseline_dir" && "$s" == "$baseline_dir" ]]; then
    continue
  fi
  name="$(printf 'run_%03d_%s' "$idx" "$(basename "$s")")"
  do_link "$s" "$DEST/phase1/$name"
  log "phase1: $s → $DEST/phase1/$name"
  idx=$((idx + 1))
done

if [[ -n "$baseline_dir" ]]; then
  do_link "$baseline_dir" "$DEST/phase2/baseline/$(basename "$baseline_dir")"
  log "phase2/baseline: $baseline_dir"
else
  log "no baseline inferred (need at least one delay dir + static dir before it)"
fi

if [[ -n "$delay_dir" ]]; then
  do_link "$delay_dir" "$DEST/phase2/delay/$(basename "$delay_dir")"
  log "phase2/delay: $delay_dir"
fi

if [[ -n "$loss_dir" ]]; then
  do_link "$loss_dir" "$DEST/phase2/loss/$(basename "$loss_dir")"
  log "phase2/loss: $loss_dir"
fi

cat > "$DEST/README.txt" <<EOF
Organized from: $LOG_ROOT
Mode: $MODE
batch: $BATCH_NAME

phase1/       — static vm_run_* (excluding inferred Phase-2 baseline)
phase2/baseline/ — last static run before first delay dir (if detected)
phase2/delay/    — directory containing tc_delay_*.log
phase2/loss/     — directory containing tc_loss_*.log

If Phase 1 count does not match expectations, re-run with a clean logs_exp or use session mode in run_experiment_matrix.sh.
EOF

log "done → $DEST"
echo "$DEST"
