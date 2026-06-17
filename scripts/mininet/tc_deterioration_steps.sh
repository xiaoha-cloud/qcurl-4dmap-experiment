#!/usr/bin/env bash
# Combined delay+loss deterioration steps on one interface (Fig.8-style).
# Applies both parameters in a single root netem qdisc so delay and loss never overwrite each other.
#
# Profile format:
#   IFACE=h2-eth1
#   <at_sec> <delay_ms> <loss_pct>
#
# Example:
#   0 20ms 0%
#   90 80ms 0.05%
#   100 20ms 0%
set -euo pipefail

PROFILE="${1:?usage: tc_deterioration_steps.sh /path/to/profile}"

IFACE=""
declare -a T_AT=()
declare -a T_DELAY=()
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
  if [[ "$line" =~ ^([0-9]+)[[:space:]]+([0-9]+)(ms)?[[:space:]]+([0-9.]+)(%)?$ ]]; then
    T_AT+=("${BASH_REMATCH[1]}")
    T_DELAY+=("${BASH_REMATCH[2]}ms")
    T_LOSS+=("${BASH_REMATCH[4]}%")
    continue
  fi
  echo "tc_deterioration: invalid profile line: ${line}" >&2
  exit 1
done < "$PROFILE"

[[ -n "$IFACE" ]] || { echo "tc_deterioration: IFACE= missing in $PROFILE" >&2; exit 1; }
((${#T_AT[@]} > 0)) || { echo "tc_deterioration: no steps in $PROFILE" >&2; exit 1; }

prev_at=-1
for at in "${T_AT[@]}"; do
  if ((at < prev_at)); then
    echo "tc_deterioration: step times must be non-decreasing; got ${at}s after ${prev_at}s in $PROFILE" >&2
    exit 1
  fi
  prev_at=$at
done

log() { echo "[$(date -Iseconds)] [tc_deterioration] $*" >&2; }

log "profile=${PROFILE}"
log "parsed IFACE=${IFACE} step_count=${#T_AT[@]}"
for i in "${!T_AT[@]}"; do
  log "profile_step[$i] at=${T_AT[$i]}s delay=${T_DELAY[$i]} loss=${T_LOSS[$i]}"
done

apply_deterioration() {
  local delay="$1"
  local loss="$2"
  log "tc qdisc replace dev ${IFACE} root netem delay ${delay} loss ${loss}"
  tc qdisc replace dev "$IFACE" root netem delay "$delay" loss "$loss" 2>/dev/null \
    || { tc qdisc del dev "$IFACE" root 2>/dev/null || true
         log "tc qdisc add dev ${IFACE} root netem delay ${delay} loss ${loss}"
         tc qdisc add dev "$IFACE" root netem delay "$delay" loss "$loss"; }
  log "applied delay=${delay} loss=${loss} dev=${IFACE}"
  log "tc -s qdisc show dev ${IFACE}:"
  tc -s qdisc show dev "$IFACE" >&2
}

prev=0
for i in "${!T_AT[@]}"; do
  at="${T_AT[$i]}"
  delay="${T_DELAY[$i]}"
  loss="${T_LOSS[$i]}"
  sleep_sec=$((at - prev))
  if ((sleep_sec > 0)); then sleep "$sleep_sec"; fi
  log "step $((i + 1))/${#T_AT[@]} at=${at}s delay=${delay} loss=${loss} dev=${IFACE}"
  apply_deterioration "$delay" "$loss"
  prev=$at
done
log "finished all steps"
