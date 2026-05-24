#!/usr/bin/env bash
# ARCHIVED — not part of final Q-ACCeSS-T workflow. Use ../run_qaccess_t_eval_matrix.sh instead.
#
# Batch runner for mp_topo.py: Phase 1 (static) + Phase 2 (fixed T/D/L + dynamic tc) + Phase 3 (auto).
#
# Naming (do not confuse):
#   --utility-mode T|D|L|auto|learn|baseline = 4D-MAP mode (baseline = controller off, learn = pg weights)
#   --scenario default|t|d|l|d_queue = Mininet TCLink preset (d_queue = like d + small path-B queue)
#   --dynamic-delay-profile  = Phase 2/3: delay steps on path B only (one run)
#   --dynamic-loss-profile   = Phase 2/3: loss steps (profile sets IFACE or IFACES; default = both paths)
#
# Phase 3 = design “layer C”: only --utility-mode auto (adaptive), on static scenarios and/or same tc profiles as Phase 2.
#
# Log directory tree (USE_SESSION=1, one batch = one session folder):
#
#   logs_exp/session_YYYYMMDD_HHMMSS/
#     phase1_default_baseline/          # one subfolder per matrix cell (--run-label)
#       server_<RUN_ID>.log             # qserver on h2
#       pull_<RUN_ID>.log               # pull client on h1
#       push_<RUN_ID>.log               # push client on h1
#     phase1_default_T/
#       server_<RUN_ID>.log
#       pull_<RUN_ID>.log
#       push_<RUN_ID>.log
#     phase2_delay_T/
#       pull_*.log push_*.log server_*.log
#       tc_delay_<RUN_ID>.log           # dynamic tc script log (Phase 2 delay only)
#     phase3_static_default_auto/
#     phase3_delay_auto/
#     phase3_loss_auto/
#
# Classification is by subfolder name (scenario + utility), not by merging streams into one file.
# The three QUIC role logs stay separate so you can grep pull vs server without a splitter.
# Optional: mp_topo.py --bg-iperf adds iperf_server_<RUN_ID>.log and iperf_client_<RUN_ID>.log in the same folder.
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
# Quick preset: 1 = skip the full matrix; run only Phase 2 LOSS (T,D,L × LOSS_PROFILE) + Phase 3 LOSS (auto).
# When 1, the RUN_PHASE* flags below are overridden after this block.
RUN_LOSS_ONLY=0

# Phase 1: loop static SCENARIOS × UTILITIES (each run: one subdir, three QUIC logs + optional tc/iperf)
RUN_PHASE1=1
PHASE1_SCENARIOS=(default t d l) # append d_queue for queueing-delay-style path B (see mp_topo SCENARIOS)
PHASE1_UTILS=(baseline T D L learn)
TIMEOUT_PHASE1=90

# Phase 2: dynamic perturbation batch (disabled by default to keep runs simple)
RUN_PHASE2_BASELINE=0
RUN_PHASE2_DELAY=0
RUN_PHASE2_LOSS=0
PHASE2_SCENARIO=default
PHASE2_UTILS=(T D L learn)
TIMEOUT_PHASE2=120
DELAY_PROFILE="$ROOT/scripts/mininet/delay_profile.example.env"
# Scheme A (dual-link netem): same loss on h1-eth0 + h1-eth1. For path-B-only, use loss_profile.example.env.
LOSS_PROFILE="$ROOT/scripts/mininet/loss_profile.both_paths.example.env"

# Phase 3: adaptive utility batch (disabled by default; enable only if needed)
RUN_PHASE3_STATIC=0
RUN_PHASE3_DELAY=0
RUN_PHASE3_LOSS=0
PHASE3_SCENARIOS=(default t d l) # empty array + RUN_PHASE3_STATIC=0 to skip static auto sweep
PHASE3_SCENARIO=default        # for dynamic delay/loss; set same as PHASE2_SCENARIO for A/B with Phase 2
TIMEOUT_PHASE3=120

# 1 = all runs under logs_exp/session_<timestamp>/…  ; 0 = flat logs_exp/vm_run_<RUN_ID>/
USE_SESSION=1

# 0 = discard verbose role/tc/tcpdump/tshark logs; set SAVE_LOGS=1 when debugging.
SAVE_LOGS="${SAVE_LOGS:-0}"

if [[ "$RUN_LOSS_ONLY" -eq 1 ]]; then
  RUN_PHASE1=0
  RUN_PHASE2_BASELINE=0
  RUN_PHASE2_DELAY=0
  RUN_PHASE2_LOSS=1
  RUN_PHASE3_STATIC=0
  RUN_PHASE3_DELAY=0
  RUN_PHASE3_LOSS=1
fi
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
  if [[ "$SAVE_LOGS" != "1" ]]; then
    MP_EXTRA+=(--disable-logs)
    log "SAVE_LOGS=0 (runtime logs disabled; set SAVE_LOGS=1 to keep them)"
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

phase3_static() {
  local sc
  for sc in "${PHASE3_SCENARIOS[@]}"; do
    log "PHASE3 STATIC begin scenario=$sc utility=auto"
    if [[ "$USE_SESSION" -eq 1 ]]; then
      mp_run --run-label "phase3_static_${sc}_auto" -- --run-exp --scenario "$sc" --utility-mode auto \
        --timeout "$TIMEOUT_PHASE3"
    else
      python3 "$MP" --run-exp --scenario "$sc" --utility-mode auto --timeout "$TIMEOUT_PHASE3"
    fi
    log "PHASE3 STATIC done scenario=$sc"
  done
}

phase3_delay() {
  log "PHASE3 DELAY begin utility=auto scenario=$PHASE3_SCENARIO"
  if [[ "$USE_SESSION" -eq 1 ]]; then
    mp_run --run-label "phase3_delay_auto" -- --run-exp --scenario "$PHASE3_SCENARIO" --utility-mode auto \
      --timeout "$TIMEOUT_PHASE3" --dynamic-delay-profile "$DELAY_PROFILE"
  else
    python3 "$MP" --run-exp --scenario "$PHASE3_SCENARIO" --utility-mode auto \
      --timeout "$TIMEOUT_PHASE3" --dynamic-delay-profile "$DELAY_PROFILE"
  fi
  log "PHASE3 DELAY done"
}

phase3_loss() {
  log "PHASE3 LOSS begin utility=auto scenario=$PHASE3_SCENARIO"
  if [[ "$USE_SESSION" -eq 1 ]]; then
    mp_run --run-label "phase3_loss_auto" -- --run-exp --scenario "$PHASE3_SCENARIO" --utility-mode auto \
      --timeout "$TIMEOUT_PHASE3" --dynamic-loss-profile "$LOSS_PROFILE"
  else
    python3 "$MP" --run-exp --scenario "$PHASE3_SCENARIO" --utility-mode auto \
      --timeout "$TIMEOUT_PHASE3" --dynamic-loss-profile "$LOSS_PROFILE"
  fi
  log "PHASE3 LOSS done"
}

main() {
  require_sudo
  cd "$ROOT"
  log "ROOT=$ROOT"
  [[ -f "$MP" ]] || { log "missing $MP"; exit 1; }
  if { [[ "$RUN_PHASE2_DELAY" -eq 1 ]] || [[ "$RUN_PHASE3_DELAY" -eq 1 ]]; } && [[ ! -f "$DELAY_PROFILE" ]]; then
    log "missing DELAY_PROFILE=$DELAY_PROFILE"; exit 1
  fi
  if { [[ "$RUN_PHASE2_LOSS" -eq 1 ]] || [[ "$RUN_PHASE3_LOSS" -eq 1 ]]; } && [[ ! -f "$LOSS_PROFILE" ]]; then
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
  if [[ "$RUN_PHASE3_STATIC" -eq 1 ]]; then
    phase3_static
  fi
  if [[ "$RUN_PHASE3_DELAY" -eq 1 ]]; then
    phase3_delay
  fi
  if [[ "$RUN_PHASE3_LOSS" -eq 1 ]]; then
    phase3_loss
  fi
  log "all enabled stages finished"
  if [[ -n "${SESSION_DIR:-}" ]]; then
    log "logs under: $ROOT/$SESSION_DIR"
  fi
}

main "$@"
