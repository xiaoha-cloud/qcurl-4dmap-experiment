#!/usr/bin/env bash
# Optional diagnostic: delay-only steps on one interface (not the main Fig.8 experiment).
# Profile format:
#   IFACE=h2-eth1
#   <at_sec> <delay_ms>
set -euo pipefail

PROFILE="${1:?usage: tc_delay_steps.sh /path/to/profile}"
FIXED_BW_MBIT="${TC_DELAY_FIXED_BW_MBIT:-}"
FIXED_LOSS_PERCENT="${TC_DELAY_FIXED_LOSS_PERCENT:-}"
COMPOSITE_QDISC=0

if [[ -n "$FIXED_BW_MBIT" || -n "$FIXED_LOSS_PERCENT" ]]; then
  [[ "$FIXED_BW_MBIT" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
    echo "tc_delay_steps: TC_DELAY_FIXED_BW_MBIT must be a positive number" >&2
    exit 2
  }
  [[ "$FIXED_LOSS_PERCENT" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
    echo "tc_delay_steps: TC_DELAY_FIXED_LOSS_PERCENT must be a non-negative number" >&2
    exit 2
  }
  COMPOSITE_QDISC=1
fi

IFACE=""
declare -a T_AT=()
declare -a T_DELAY=()

while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [[ -z "$line" ]] && continue
  if [[ "$line" =~ ^IFACE=(.+)$ ]]; then
    IFACE="${BASH_REMATCH[1]}"
    continue
  fi
  if [[ "$line" =~ ^([0-9]+)[[:space:]]+([0-9]+)(ms)?$ ]]; then
    T_AT+=("${BASH_REMATCH[1]}")
    T_DELAY+=("${BASH_REMATCH[2]}ms")
  fi
done < "$PROFILE"

[[ -n "$IFACE" ]] || { echo "tc_delay_steps: IFACE= missing in $PROFILE" >&2; exit 1; }
((${#T_AT[@]} > 0)) || { echo "tc_delay_steps: no steps in $PROFILE" >&2; exit 1; }

log() { echo "[$(date -Iseconds)] [tc_delay] $*" >&2; }

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
  log "verification_ok: root_tbf=1: fixed_bw=${FIXED_BW_MBIT}mbit child_netem=10: parent=1:1 dynamic_delay fixed_loss=${FIXED_LOSS_PERCENT}%"
}

log "profile=${PROFILE}"
log "parsed IFACE=${IFACE} step_count=${#T_AT[@]}"
log "composite_qdisc=${COMPOSITE_QDISC} fixed_bw_mbit=${FIXED_BW_MBIT:-none} fixed_loss_percent=${FIXED_LOSS_PERCENT:-none}"
for i in "${!T_AT[@]}"; do
  log "profile_step[$i] at=${T_AT[$i]}s delay=${T_DELAY[$i]}"
done

apply_delay() {
  local delay="$1"
  if [[ "$COMPOSITE_QDISC" == "1" ]]; then
    log "tc_cmd_root: tc qdisc replace dev ${IFACE} root handle 1: tbf rate ${FIXED_BW_MBIT}mbit burst 64kbit latency 400ms"
    tc qdisc replace dev "$IFACE" root handle 1: tbf rate "${FIXED_BW_MBIT}mbit" burst 64kbit latency 400ms
    log "tc_cmd_child: tc qdisc replace dev ${IFACE} parent 1:1 handle 10: netem delay ${delay} loss ${FIXED_LOSS_PERCENT}%"
    tc qdisc replace dev "$IFACE" parent 1:1 handle 10: netem delay "$delay" loss "${FIXED_LOSS_PERCENT}%"
    verify_composite_qdisc
    log_tc_state "after_${delay}"
    return 0
  fi
  tc qdisc replace dev "$IFACE" root netem delay "$delay" 2>/dev/null \
    || { tc qdisc del dev "$IFACE" root 2>/dev/null || true
         tc qdisc add dev "$IFACE" root netem delay "$delay"; }
  log "applied delay=${delay} dev=${IFACE}"
}

prev=0
if [[ "$COMPOSITE_QDISC" == "1" ]]; then
  log_tc_state "before_first_step"
fi
for i in "${!T_AT[@]}"; do
  at="${T_AT[$i]}"
  delay="${T_DELAY[$i]}"
  sleep_sec=$((at - prev))
  if ((sleep_sec > 0)); then sleep "$sleep_sec"; fi
  log "step $((i + 1))/${#T_AT[@]} at=${at}s delay=${delay} dev=${IFACE}"
  apply_delay "$delay"
  prev=$at
done
log "finished all steps"
