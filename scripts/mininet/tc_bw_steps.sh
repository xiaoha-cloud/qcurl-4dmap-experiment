#!/usr/bin/env bash
# Phase 2 (bandwidth-only): step egress rate on one interface.
# For server->client bulk (pull), put IFACE on the server side (e.g. h2-eth1) so data is shaped.
# Timeline is relative to when this script starts (before server/pull/push in --run-exp).
# Profile format: see bw_profile.example.env
set -euo pipefail

PROFILE="${1:?usage: tc_bw_steps.sh /path/to/profile}"

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

apply_bw() {
  local mbit="$1"
  # Use TBF to force egress shaping to the target capacity.
  if tc qdisc replace dev "$IFACE" root tbf rate "${mbit}mbit" burst 64kbit latency 400ms 2>/dev/null; then
    return 0
  fi
  log "replace failed, trying del+add (may drop Mininet TCLink qdisc on this iface)"
  tc qdisc del dev "$IFACE" root 2>/dev/null || true
  tc qdisc add dev "$IFACE" root tbf rate "${mbit}mbit" burst 64kbit latency 400ms
}

prev=0
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
