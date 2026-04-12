#!/usr/bin/env bash
# Phase 2 (loss-only): fixed delay + step netem loss on one or more interfaces.
# Default profile uses path B only (IFACE=h1-eth1). For multipath QUIC to "see" loss,
# use IFACES=h1-eth0,h1-eth1 (same loss/delay on both links — scheme A).
# Timeline is relative to when this script starts (before server/pull/push in --run-exp).
# Profile format: see loss_profile.example.env, loss_profile.both_paths.example.env
set -euo pipefail

PROFILE="${1:?usage: tc_loss_steps.sh /path/to/profile}"

IFACE=""
IFACES_CSV=""
FIXED_DELAY_MS=20
declare -a T_AT=()
declare -a T_LOSS=()

while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [[ -z "$line" ]] && continue
  if [[ "$line" =~ ^IFACES=(.+)$ ]]; then
    IFACES_CSV="${BASH_REMATCH[1]}"
    continue
  fi
  if [[ "$line" =~ ^IFACE=(.+)$ ]]; then
    IFACE="${BASH_REMATCH[1]}"
    continue
  fi
  if [[ "$line" =~ ^FIXED_DELAY_MS=([0-9]+)$ ]]; then
    FIXED_DELAY_MS="${BASH_REMATCH[1]}"
    continue
  fi
  if [[ "$line" =~ ^([0-9]+)[[:space:]]+([0-9]+(\.[0-9]+)?)$ ]]; then
    T_AT+=("${BASH_REMATCH[1]}")
    T_LOSS+=("${BASH_REMATCH[2]}")
  fi
done < "$PROFILE"

declare -a IFACE_LIST=()
if [[ -n "$IFACES_CSV" ]]; then
  IFS=',' read -r -a IFACE_LIST <<< "${IFACES_CSV// /}"
elif [[ -n "$IFACE" ]]; then
  IFACE_LIST=("$IFACE")
else
  echo "tc_loss_steps: set IFACES= (comma list) or IFACE= in $PROFILE" >&2
  exit 1
fi
((${#IFACE_LIST[@]} > 0)) || { echo "tc_loss_steps: empty interface list in $PROFILE" >&2; exit 1; }
((${#T_AT[@]} > 0)) || { echo "tc_loss_steps: no steps in $PROFILE" >&2; exit 1; }

ifaces_joined() { local IFS=+; echo "${IFACE_LIST[*]}"; }

log() { echo "[$(date -Iseconds)] [tc_loss] $*" >&2; }

apply_loss() {
  local loss="$1"
  local dev
  for dev in "${IFACE_LIST[@]}"; do
    dev="${dev// /}"
    [[ -z "$dev" ]] && continue
    if tc qdisc replace dev "$dev" root netem delay "${FIXED_DELAY_MS}ms" loss "${loss}%" 2>/dev/null; then
      continue
    fi
    log "replace failed on $dev, trying del+add (may drop Mininet TCLink shaping on this iface)"
    tc qdisc del dev "$dev" root 2>/dev/null || true
    tc qdisc add dev "$dev" root netem delay "${FIXED_DELAY_MS}ms" loss "${loss}%"
  done
}

prev=0
for i in "${!T_AT[@]}"; do
  at="${T_AT[$i]}"
  loss="${T_LOSS[$i]}"
  sleep_sec=$((at - prev))
  if ((sleep_sec > 0)); then sleep "$sleep_sec"; fi
  log "step $((i + 1))/${#T_AT[@]} at=${at}s loss=${loss}% delay=${FIXED_DELAY_MS}ms dev=$(ifaces_joined)"
  apply_loss "$loss"
  prev=$at
done
log "finished all steps (netem state left applied)"
