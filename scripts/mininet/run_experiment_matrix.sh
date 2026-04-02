#!/usr/bin/env bash
# Batch runner for mp_topo.py: Phase 1 (static scenarios) + Phase 2 (dynamic tc).
#
# Naming (do not confuse):
#   --utility-mode T|D|L     = 4D-MAP optimization preference
#   --scenario default|t|d|l = Mininet TCLink preset at build time (static)
#   --dynamic-delay-profile  = Phase 2: delay steps on path B only (one run)
#   --dynamic-loss-profile   = Phase 2: loss steps on path B only (one run)
#
# Logs: each --run-exp creates logs_exp/vm_run_<RUN_ID>/ with 3 files always
#   (server_*.log, pull_*.log, push_*.log). Dynamic tc adds a 4th (tc_delay_* or tc_loss_*).
#
# Paper-style Phase 2 triple (baseline + delay + loss), same PHASE2_SCENARIO / utility:
#   baseline: no --dynamic-*  → 3 files per run
#   delay:    --dynamic-delay-profile → 4 files
#   loss:     --dynamic-loss-profile  → 4 files
#   → 11 files total in 3 vm_run_* dirs (set RUN_PHASE1=0 to run only this block).
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
PHASE2_UTILS=(T) # set to (T D L) to repeat each phase2 arm per utility
TIMEOUT_PHASE2=120
DELAY_PROFILE="$ROOT/scripts/mininet/delay_profile.example.env"
LOSS_PROFILE="$ROOT/scripts/mininet/loss_profile.example.env"
# ---------------------------------------------------------

log() { echo "[$(date -Iseconds)] $*" >&2; }

require_sudo() {
  if [[ "$(id -u)" -ne 0 ]]; then
    log "re-run with sudo (Mininet needs root)"
    exit 1
  fi
}

phase1() {
  local sc um
  for sc in "${PHASE1_SCENARIOS[@]}"; do
    for um in "${PHASE1_UTILS[@]}"; do
      log "PHASE1 begin scenario=$sc utility=$um"
      python3 "$MP" --run-exp --scenario "$sc" --utility-mode "$um" --timeout "$TIMEOUT_PHASE1"
      log "PHASE1 done  scenario=$sc utility=$um"
    done
  done
}

phase2_baseline() {
  local um
  for um in "${PHASE2_UTILS[@]}"; do
    log "PHASE2 BASELINE (no dynamic tc) begin utility=$um scenario=$PHASE2_SCENARIO"
    python3 "$MP" --run-exp --scenario "$PHASE2_SCENARIO" --utility-mode "$um" \
      --timeout "$TIMEOUT_PHASE2"
    log "PHASE2 BASELINE done utility=$um"
  done
}

phase2_delay() {
  local um
  for um in "${PHASE2_UTILS[@]}"; do
    log "PHASE2 DELAY begin utility=$um scenario=$PHASE2_SCENARIO"
    python3 "$MP" --run-exp --scenario "$PHASE2_SCENARIO" --utility-mode "$um" \
      --timeout "$TIMEOUT_PHASE2" --dynamic-delay-profile "$DELAY_PROFILE"
    log "PHASE2 DELAY done utility=$um"
  done
}

phase2_loss() {
  local um
  for um in "${PHASE2_UTILS[@]}"; do
    log "PHASE2 LOSS begin utility=$um scenario=$PHASE2_SCENARIO"
    python3 "$MP" --run-exp --scenario "$PHASE2_SCENARIO" --utility-mode "$um" \
      --timeout "$TIMEOUT_PHASE2" --dynamic-loss-profile "$LOSS_PROFILE"
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
}

main "$@"
