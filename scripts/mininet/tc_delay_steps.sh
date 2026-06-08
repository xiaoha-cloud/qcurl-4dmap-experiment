#!/usr/bin/env bash
# Optional diagnostic: delay-only steps on one interface (not the main Fig.8 experiment).
# Profile format:
#   IFACE=h2-eth1
#   <at_sec> <delay_ms>
set -euo pipefail

PROFILE="${1:?usage: tc_delay_steps.sh /path/to/profile}"

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

log "profile=${PROFILE}"
log "parsed IFACE=${IFACE} step_count=${#T_AT[@]}"
for i in "${!T_AT[@]}"; do
  log "profile_step[$i] at=${T_AT[$i]}s delay=${T_DELAY[$i]}"
done

apply_delay() {
  local delay="$1"
  tc qdisc replace dev "$IFACE" root netem delay "$delay" 2>/dev/null \
    || { tc qdisc del dev "$IFACE" root 2>/dev/null || true
         tc qdisc add dev "$IFACE" root netem delay "$delay"; }
  log "applied delay=${delay} dev=${IFACE}"
}

prev=0
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
