#!/usr/bin/env bash
# Optional diagnostic: loss-only steps on one interface (not the main Fig.8 experiment).
# Profile format:
#   IFACE=h2-eth1
#   <at_sec> <loss_pct>
set -euo pipefail

PROFILE="${1:?usage: tc_loss_steps.sh /path/to/profile}"
FIXED_BW_MBIT="${TC_LOSS_FIXED_BW_MBIT:-}"
FIXED_DELAY_MS="${TC_LOSS_FIXED_DELAY_MS:-}"
COMPOSITE_QDISC=0

if [[ -n "$FIXED_BW_MBIT" || -n "$FIXED_DELAY_MS" ]]; then
  [[ "$FIXED_BW_MBIT" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
    echo "tc_loss_steps: TC_LOSS_FIXED_BW_MBIT must be a positive number" >&2
    exit 2
  }
  [[ "$FIXED_DELAY_MS" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
    echo "tc_loss_steps: TC_LOSS_FIXED_DELAY_MS must be a non-negative number" >&2
    exit 2
  }
  COMPOSITE_QDISC=1
fi

IFACE=""
declare -a T_AT=()
declare -a T_LOSS=()

while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [[ -z "$line" ]] && continue
  if [[ "$line" =~ ^IFACE=(.+)$ ]]; then
    IFACE="${BASH_REMATCH[1]}"
    continue
  fi
  if [[ "$line" =~ ^([0-9]+)[[:space:]]+([0-9.]+)(%)?$ ]]; then
    T_AT+=("${BASH_REMATCH[1]}")
    T_LOSS+=("${BASH_REMATCH[2]}%")
  fi
done < "$PROFILE"

[[ -n "$IFACE" ]] || { echo "tc_loss_steps: IFACE= missing in $PROFILE" >&2; exit 1; }
((${#T_AT[@]} > 0)) || { echo "tc_loss_steps: no steps in $PROFILE" >&2; exit 1; }

log() { echo "[$(date -Iseconds)] [tc_loss] $*" >&2; }

log_tc_state() {
  local stage="$1"
  tc -s qdisc show dev "$IFACE" 2>&1 | while IFS= read -r line; do
    log "qdisc_state stage=${stage}: ${line}"
  done
  tc -s class show dev "$IFACE" 2>&1 | while IFS= read -r line; do
    log "class_state stage=${stage}: ${line}"
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
  log "verification_ok: root_tbf=1: fixed_bw=${FIXED_BW_MBIT}mbit child_netem=10: parent=1:1 fixed_delay=${FIXED_DELAY_MS}ms dynamic_loss"
}

log "profile=${PROFILE}"
log "parsed IFACE=${IFACE} step_count=${#T_AT[@]}"
log "composite_qdisc=${COMPOSITE_QDISC} fixed_bw_mbit=${FIXED_BW_MBIT:-none} fixed_delay_ms=${FIXED_DELAY_MS:-none}"
for i in "${!T_AT[@]}"; do
  log "profile_step[$i] at=${T_AT[$i]}s loss=${T_LOSS[$i]}"
done

apply_loss() {
  local loss="$1"
  if [[ "$COMPOSITE_QDISC" == "1" ]]; then
    log "tc_cmd_root: tc qdisc replace dev ${IFACE} root handle 1: tbf rate ${FIXED_BW_MBIT}mbit burst 64kbit latency 400ms"
    tc qdisc replace dev "$IFACE" root handle 1: tbf rate "${FIXED_BW_MBIT}mbit" burst 64kbit latency 400ms
    log "tc_cmd_child: tc qdisc replace dev ${IFACE} parent 1:1 handle 10: netem delay ${FIXED_DELAY_MS}ms loss ${loss}"
    tc qdisc replace dev "$IFACE" parent 1:1 handle 10: netem delay "${FIXED_DELAY_MS}ms" loss "$loss"
    verify_composite_qdisc
    log_tc_state "after_${loss}"
    return 0
  fi
  tc qdisc replace dev "$IFACE" root netem loss "$loss" 2>/dev/null \
    || { tc qdisc del dev "$IFACE" root 2>/dev/null || true
         tc qdisc add dev "$IFACE" root netem loss "$loss"; }
  log "applied loss=${loss} dev=${IFACE}"
}

prev=0
if [[ "$COMPOSITE_QDISC" == "1" ]]; then
  log_tc_state "before_first_step"
fi
for i in "${!T_AT[@]}"; do
  at="${T_AT[$i]}"
  loss="${T_LOSS[$i]}"
  sleep_sec=$((at - prev))
  if ((sleep_sec > 0)); then sleep "$sleep_sec"; fi
  log "step $((i + 1))/${#T_AT[@]} at=${at}s loss=${loss} dev=${IFACE}"
  apply_loss "$loss"
  prev=$at
done
log "finished all steps"
