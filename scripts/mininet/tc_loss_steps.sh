#!/usr/bin/env bash
# Optional diagnostic: loss-only steps on one interface (not the main Fig.8 experiment).
# Profile format:
#   IFACE=h2-eth1
#   <at_sec> <loss_pct>
set -euo pipefail

PROFILE="${1:?usage: tc_loss_steps.sh /path/to/profile}"

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

log "profile=${PROFILE}"
log "parsed IFACE=${IFACE} step_count=${#T_AT[@]}"
for i in "${!T_AT[@]}"; do
  log "profile_step[$i] at=${T_AT[$i]}s loss=${T_LOSS[$i]}"
done

apply_loss() {
  local loss="$1"
  tc qdisc replace dev "$IFACE" root netem loss "$loss" 2>/dev/null \
    || { tc qdisc del dev "$IFACE" root 2>/dev/null || true
         tc qdisc add dev "$IFACE" root netem loss "$loss"; }
  log "applied loss=${loss} dev=${IFACE}"
}

prev=0
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
