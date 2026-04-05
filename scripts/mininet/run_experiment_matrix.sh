#!/usr/bin/env bash
# Batch runner for mp_topo.py: Phase 1 (static scenarios) + Phase 2 (dynamic tc).
#
# Naming (do not confuse):
#   --utility-mode T|D|L|auto = 4D-MAP optimization preference (auto = runtime selector)
#   --scenario default|t|d|l = Mininet TCLink preset at build time (static)
#   --dynamic-delay-profile  = Phase 2: delay steps on path B only (one run)
#   --dynamic-loss-profile   = Phase 2: loss steps on path B only (one run)
#
# Logs: each --run-exp creates a directory under logs_exp/session_<TS>/:
#   --run-label phase1_<scenario>_<utility>   (Phase 1)
#   --run-label phase2_baseline_<utility>
#   --run-label phase2_delay_<utility>
#   --run-label phase2_loss_<utility>
# Files inside are still server_<RUN_ID>.log, pull_<RUN_ID>.log, push_<RUN_ID>.log
# (+ tc_* for dynamic runs). RUN_ID is the timestamp when that run started.
#
# Set USE_SESSION=0 to restore legacy flat layout: logs_exp/vm_run_<RUN_ID>/ only.
#
# Usage (VM, repo root):
#   chmod +x scripts/mininet/run_experiment_matrix.sh
#   sudo ./scripts/mininet/run_experiment_matrix.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"

# -------------------- tune this block --------------------
# Phase 1: loop static SCENARIOS × UTILITIES (each run: 3 log files under vm_run_*)
RUN_PHASE1=1
PHASE1_SCENARIOS=(default t d l)
PHASE1_UTILS=(T D L)
TIMEOUT_PHASE1=90

# Phase 2: optional static baseline (no dynamic tc), then delay steps, then loss steps
RUN_PHASE2_BASELINE=1
RUN_PHASE2_DELAY=1
RUN_PHASE2_LOSS=1
PHASE2_SCENARIO=default
PHASE2_UTILS=(T D L) # add "auto" to also run adaptive utility-mode
TIMEOUT_PHASE2=120
DELAY_PROFILE="$ROOT/scripts/mininet/delay_profile.example.env"
LOSS_PROFILE="$ROOT/scripts/mininet/loss_profile.example.env"

# 1 = all runs under logs_exp/session_<timestamp>/…  ; 0 = flat logs_exp/vm_run_<RUN_ID>/
USE_SESSION=1
# ---------------------------------------------------------

log() { echo "[$(date -Iseconds)] $*" >&2; }

require_sudo() {
  if [[ "$(id -u)" -ne 0 ]]; then
    log "re-run with sudo (Mininet needs root)"
    exit 1
  fi
}

init_session() {
  SESSION_DIR=""
  MP_EXTRA=()
  if [[ "$USE_SESSION" -eq 1 ]]; then
    SESSION_DIR="logs_exp/session_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$ROOT/$SESSION_DIR"
    MP_EXTRA=(--log-parent "$SESSION_DIR")
    log "SESSION_DIR=$SESSION_DIR (all runs this batch go here)"
    mkdir -p "$ROOT/logs_exp"
    echo "$SESSION_DIR" > "$ROOT/logs_exp/.last_session"
  fi
}

mp_run() {
  # usage: mp_run [-- LABEL_ARGS...] -- EXTRA_ARGS_TO_PYTHON...
  local label_args=()
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--" ]]; then
      shift
      break
    fi
    label_args+=("$1")
    shift
  done
  python3 "$MP" "${MP_EXTRA[@]}" "${label_args[@]}" "$@"
}

phase1() {
  local sc um
  for sc in "${PHASE1_SCENARIOS[@]}"; do
    for um in "${PHASE1_UTILS[@]}"; do
      log "PHASE1 begin scenario=$sc utility=$um"
      if [[ "$USE_SESSION" -eq 1 ]]; then
        mp_run --run-label "phase1_${sc}_${um}" -- --run-exp --scenario "$sc" --utility-mode "$um" \
          --timeout "$TIMEOUT_PHASE1"
      else
        python3 "$MP" --run-exp --scenario "$sc" --utility-mode "$um" --timeout "$TIMEOUT_PHASE1"
      fi
      log "PHASE1 done  scenario=$sc utility=$um"
    done
  done
}

phase2_baseline() {
  local um
  for um in "${PHASE2_UTILS[@]}"; do
    log "PHASE2 BASELINE (no dynamic tc) begin utility=$um scenario=$PHASE2_SCENARIO"
    if [[ "$USE_SESSION" -eq 1 ]]; then
      mp_run --run-label "phase2_baseline_${um}" -- --run-exp --scenario "$PHASE2_SCENARIO" --utility-mode "$um" \
        --timeout "$TIMEOUT_PHASE2"
    else
      python3 "$MP" --run-exp --scenario "$PHASE2_SCENARIO" --utility-mode "$um" \
        --timeout "$TIMEOUT_PHASE2"
    fi
    log "PHASE2 BASELINE done utility=$um"
  done
}

phase2_delay() {
  local um
  for um in "${PHASE2_UTILS[@]}"; do
    log "PHASE2 DELAY begin utility=$um scenario=$PHASE2_SCENARIO"
    if [[ "$USE_SESSION" -eq 1 ]]; then
      mp_run --run-label "phase2_delay_${um}" -- --run-exp --scenario "$PHASE2_SCENARIO" --utility-mode "$um" \
        --timeout "$TIMEOUT_PHASE2" --dynamic-delay-profile "$DELAY_PROFILE"
    else
      python3 "$MP" --run-exp --scenario "$PHASE2_SCENARIO" --utility-mode "$um" \
        --timeout "$TIMEOUT_PHASE2" --dynamic-delay-profile "$DELAY_PROFILE"
    fi
    log "PHASE2 DELAY done utility=$um"
  done
}

phase2_loss() {
  local um
  for um in "${PHASE2_UTILS[@]}"; do
    log "PHASE2 LOSS begin utility=$um scenario=$PHASE2_SCENARIO"
    if [[ "$USE_SESSION" -eq 1 ]]; then
      mp_run --run-label "phase2_loss_${um}" -- --run-exp --scenario "$PHASE2_SCENARIO" --utility-mode "$um" \
        --timeout "$TIMEOUT_PHASE2" --dynamic-loss-profile "$LOSS_PROFILE"
    else
      python3 "$MP" --run-exp --scenario "$PHASE2_SCENARIO" --utility-mode "$um" \
        --timeout "$TIMEOUT_PHASE2" --dynamic-loss-profile "$LOSS_PROFILE"
    fi
    log "PHASE2 LOSS done utility=$um"
  done
}

main() {
  require_sudo
  cd "$ROOT"
  log "ROOT=$ROOT"
  [[ -f "$MP" ]] || { log "missing $MP"; exit 1; }
  if [[ "$RUN_PHASE2_DELAY" -eq 1 ]] && [[ ! -f "$DELAY_PROFILE" ]]; then
    log "missing DELAY_PROFILE=$DELAY_PROFILE"; exit 1
  fi
  if [[ "$RUN_PHASE2_LOSS" -eq 1 ]] && [[ ! -f "$LOSS_PROFILE" ]]; then
    log "missing LOSS_PROFILE=$LOSS_PROFILE"; exit 1
  fi

  init_session

  if [[ "$RUN_PHASE1" -eq 1 ]]; then
    phase1
  fi
  if [[ "$RUN_PHASE2_BASELINE" -eq 1 ]]; then
    phase2_baseline
  fi
  if [[ "$RUN_PHASE2_DELAY" -eq 1 ]]; then
    phase2_delay
  fi
  if [[ "$RUN_PHASE2_LOSS" -eq 1 ]]; then
    phase2_loss
  fi
  log "all enabled stages finished"
  if [[ -n "${SESSION_DIR:-}" ]]; then
    log "logs under: $ROOT/$SESSION_DIR"
  fi
}

main "$@"
