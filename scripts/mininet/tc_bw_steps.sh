#!/usr/bin/env bash
# Phase 2 (bandwidth-only): step egress rate on one interface.
# For server->client bulk (pull), put IFACE on the server side (e.g. h2-eth1) so data is shaped.
# Timeline is relative to when this script starts (before server/pull/push in --run-exp).
# Profile format: see bw_profile.example.env
set -euo pipefail

PROFILE="${1:?usage: tc_bw_steps.sh /path/to/profile}"
FIXED_DELAY_MS="${TC_BW_FIXED_DELAY_MS:-}"
FIXED_LOSS_PERCENT="${TC_BW_FIXED_LOSS_PERCENT:-}"
COMPOSITE_QDISC=0

if [[ -n "$FIXED_DELAY_MS" || -n "$FIXED_LOSS_PERCENT" ]]; then
  [[ "$FIXED_DELAY_MS" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
    echo "tc_bw_steps: TC_BW_FIXED_DELAY_MS must be a non-negative number" >&2
    exit 2
  }
  [[ "$FIXED_LOSS_PERCENT" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
    echo "tc_bw_steps: TC_BW_FIXED_LOSS_PERCENT must be a non-negative number" >&2
    exit 2
  }
  COMPOSITE_QDISC=1
fi

IFACE=""
declare -a T_AT=()
declare -a T_BW=()

while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [[ -z "$line" ]] && continue
  if [[ "$line" =~ ^IFACE=(.+)$ ]]; then
    IFACE="${BASH_REMATCH[1]}"
    continue
  fi
  if [[ "$line" =~ ^([0-9]+)[[:space:]]+([0-9]+)$ ]]; then
    T_AT+=("${BASH_REMATCH[1]}")
    T_BW+=("${BASH_REMATCH[2]}")
  fi
done < "$PROFILE"

[[ -n "$IFACE" ]] || { echo "tc_bw_steps: IFACE= missing in $PROFILE" >&2; exit 1; }
((${#T_AT[@]} > 0)) || { echo "tc_bw_steps: no steps in $PROFILE" >&2; exit 1; }

log() { echo "[$(date -Iseconds)] [tc_bw] $*" >&2; }

log_tc_state() {
  local stage="$1"
  tc -s qdisc show dev "$IFACE" 2>&1 | while IFS= read -r line; do
    log "qdisc_state stage=${stage}: ${line}"
  done
  tc -s class show dev "$IFACE" 2>&1 | while IFS= read -r line; do
    log "class_state stage=${stage}: ${line}"
  done
}

log_legacy_qdisc_state() {
  tc qdisc show dev "$IFACE" 2>&1 | while IFS= read -r line; do
    log "qdisc_show: ${line}"
  done
  tc -s qdisc show dev "$IFACE" 2>&1 | while IFS= read -r line; do
    log "qdisc_stats: ${line}"
  done
}

verify_composite_qdisc() {
  local state
  state="$(tc qdisc show dev "$IFACE" 2>&1)"
  grep -Eq 'qdisc tbf 1: root' <<<"$state" || {
    log "verification_failed: missing root tbf handle 1: on ${IFACE}"
    return 1
  }
  grep -Eq 'qdisc netem 10: parent 1:1' <<<"$state" || {
    log "verification_failed: missing child netem handle 10: parent 1:1 on ${IFACE}"
    return 1
  }
  log "verification_ok: root_tbf=1: child_netem=10: parent=1:1 fixed_delay=${FIXED_DELAY_MS}ms fixed_loss=${FIXED_LOSS_PERCENT}%"
}

log "profile=${PROFILE}"
log "parsed IFACE=${IFACE} step_count=${#T_AT[@]}"
log "composite_qdisc=${COMPOSITE_QDISC} fixed_delay_ms=${FIXED_DELAY_MS:-none} fixed_loss_percent=${FIXED_LOSS_PERCENT:-none}"
for i in "${!T_AT[@]}"; do
  log "profile_step[$i] at=${T_AT[$i]}s bw=${T_BW[$i]}mbit"
done
log "timeline_origin=tc_bw_steps.sh start (sleep/s steps are relative to this moment)"

apply_bw() {
  local mbit="$1"
  if [[ "$COMPOSITE_QDISC" == "1" ]]; then
    log "tc_cmd_root: tc qdisc replace dev ${IFACE} root handle 1: tbf rate ${mbit}mbit burst 64kbit latency 400ms"
    tc qdisc replace dev "$IFACE" root handle 1: tbf rate "${mbit}mbit" burst 64kbit latency 400ms
    log "tc_cmd_child: tc qdisc replace dev ${IFACE} parent 1:1 handle 10: netem delay ${FIXED_DELAY_MS}ms loss ${FIXED_LOSS_PERCENT}%"
    tc qdisc replace dev "$IFACE" parent 1:1 handle 10: netem \
      delay "${FIXED_DELAY_MS}ms" loss "${FIXED_LOSS_PERCENT}%"
    verify_composite_qdisc
    log_tc_state "after_${mbit}mbit"
    return 0
  fi
  # Use TBF to force egress shaping to the target capacity.
  if tc qdisc replace dev "$IFACE" root tbf rate "${mbit}mbit" burst 64kbit latency 400ms 2>/dev/null; then
    log "tc_cmd: tc qdisc replace dev ${IFACE} root tbf rate ${mbit}mbit burst 64kbit latency 400ms"
    log_legacy_qdisc_state
    return 0
  fi
  log "replace failed, trying del+add (may drop Mininet TCLink qdisc on this iface)"
  tc qdisc del dev "$IFACE" root 2>/dev/null || true
  tc qdisc add dev "$IFACE" root tbf rate "${mbit}mbit" burst 64kbit latency 400ms
  log_legacy_qdisc_state
}

prev=0
if [[ "$COMPOSITE_QDISC" == "1" ]]; then
  log_tc_state "before_first_step"
fi
for i in "${!T_AT[@]}"; do
  at="${T_AT[$i]}"
  bw="${T_BW[$i]}"
  sleep_sec=$((at - prev))
  if ((sleep_sec > 0)); then sleep "$sleep_sec"; fi
  log "step $((i + 1))/${#T_AT[@]} at=${at}s bw=${bw}mbit dev=${IFACE}"
  apply_bw "$bw"
  prev=$at
done
log "finished all steps (qdisc state left applied)"
