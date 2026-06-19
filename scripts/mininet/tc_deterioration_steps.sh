#!/usr/bin/env bash
# Combined delay+loss deterioration on one interface (Fig.8-style).
# Applies delay+loss via a child netem qdisc under Mininet's existing HTB class.
# Does not replace the root HTB bandwidth hierarchy.
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
NETEM_PARENT=""
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

timeline_event() {
  local event="$1"
  shift
  [[ -z "${TIMELINE_JSONL:-}" ]] && return 0
  python3 - "$TIMELINE_JSONL" "$event" "$@" <<'PY'
import json
import sys
from datetime import datetime, timezone

path = sys.argv[1]
event = sys.argv[2]
extra = {}
for item in sys.argv[3:]:
    if "=" in item:
        key, value = item.split("=", 1)
        extra[key] = value
row = {
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "event": event,
    **extra,
}
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row) + "\n")
PY
}

log_hierarchy() {
  local label="$1"
  log "=== hierarchy ${label}: qdisc dev=${IFACE} ==="
  while IFS= read -r line; do
    [[ -n "$line" ]] && log "qdisc: ${line}"
  done < <(tc -s qdisc show dev "$IFACE" 2>&1 || true)
  log "=== hierarchy ${label}: class dev=${IFACE} ==="
  while IFS= read -r line; do
    [[ -n "$line" ]] && log "class: ${line}"
  done < <(tc -s class show dev "$IFACE" 2>&1 || true)
  log "=== hierarchy ${label}: filter dev=${IFACE} ==="
  while IFS= read -r line; do
    [[ -n "$line" ]] && log "filter: ${line}"
  done < <(tc -s filter show dev "$IFACE" 2>&1 || true)
}

detect_htb_netem_parent() {
  local root_line htb_major netem_line class_line

  root_line="$(tc qdisc show dev "$IFACE" 2>/dev/null | head -n 1 || true)"
  if [[ -z "$root_line" ]]; then
    echo "tc_deterioration: no qdisc on ${IFACE}" >&2
    return 1
  fi
  if [[ "$root_line" == *"qdisc netem"* && "$root_line" == *"root"* ]]; then
    echo "tc_deterioration: root is netem on ${IFACE}; expected root HTB with child netem" >&2
    echo "tc_deterioration: ${root_line}" >&2
    return 1
  fi
  if [[ "$root_line" != *"qdisc htb"* || "$root_line" != *"root"* ]]; then
    echo "tc_deterioration: unsupported root qdisc on ${IFACE}; expected HTB root" >&2
    echo "tc_deterioration: ${root_line}" >&2
    return 1
  fi
  if [[ "$root_line" =~ qdisc[[:space:]]+htb[[:space:]]+([0-9]+):[[:space:]]+root ]]; then
    htb_major="${BASH_REMATCH[1]}"
  else
    echo "tc_deterioration: failed to parse HTB root handle on ${IFACE}" >&2
    echo "tc_deterioration: ${root_line}" >&2
    return 1
  fi

  # Mininet TCLink uses per-interface major handles (e.g. 5:1), not always 1:1.
  netem_line="$(tc qdisc show dev "$IFACE" 2>/dev/null | grep -E "qdisc netem.*parent ${htb_major}:" | head -n 1 || true)"
  if [[ -n "$netem_line" && "$netem_line" =~ parent[[:space:]]+([0-9]+:[0-9]+) ]]; then
    NETEM_PARENT="${BASH_REMATCH[1]}"
    log "detected existing netem child; netem parent class ${NETEM_PARENT} on dev=${IFACE} (htb root ${htb_major}:)"
    return 0
  fi

  class_line="$(tc class show dev "$IFACE" 2>/dev/null | grep -E "class[[:space:]]+htb[[:space:]]+${htb_major}:[0-9]+" | head -n 1 || true)"
  if [[ -z "$class_line" ]]; then
    echo "tc_deterioration: no HTB class ${htb_major}:x found on ${IFACE}" >&2
    return 1
  fi
  if [[ "$class_line" =~ class[[:space:]]+htb[[:space:]]+([0-9]+:[0-9]+) ]]; then
    NETEM_PARENT="${BASH_REMATCH[1]}"
  else
    echo "tc_deterioration: failed to parse HTB class handle on ${IFACE}" >&2
    return 1
  fi

  log "detected root HTB ${htb_major}: with netem parent class ${NETEM_PARENT} on dev=${IFACE}"
  return 0
}

apply_deterioration() {
  local delay="$1"
  local loss="$2"

  log "tc qdisc replace dev ${IFACE} parent ${NETEM_PARENT} netem delay ${delay} loss ${loss}"
  if tc qdisc replace dev "$IFACE" parent "$NETEM_PARENT" netem delay "$delay" loss "$loss" 2>/dev/null; then
    :
  elif tc qdisc add dev "$IFACE" parent "$NETEM_PARENT" netem delay "$delay" loss "$loss" 2>/dev/null; then
    log "tc qdisc add dev ${IFACE} parent ${NETEM_PARENT} netem delay ${delay} loss ${loss}"
  else
    echo "tc_deterioration: failed to attach netem under parent ${NETEM_PARENT} on ${IFACE}" >&2
    return 1
  fi

  if ! tc qdisc show dev "$IFACE" | grep -q "parent ${NETEM_PARENT}.*netem"; then
    echo "tc_deterioration: netem child not present under ${NETEM_PARENT} after apply on ${IFACE}" >&2
    log_hierarchy "after_failed_apply"
    return 1
  fi
  if tc qdisc show dev "$IFACE" | head -n 1 | grep -q "qdisc netem.*root"; then
    echo "tc_deterioration: root became netem on ${IFACE}; HTB hierarchy was destroyed" >&2
    return 1
  fi

  log "applied delay=${delay} loss=${loss} dev=${IFACE} parent=${NETEM_PARENT}"
  return 0
}

log "profile=${PROFILE}"
log "parsed IFACE=${IFACE} step_count=${#T_AT[@]}"
for i in "${!T_AT[@]}"; do
  log "profile_step[$i] at=${T_AT[$i]}s delay=${T_DELAY[$i]} loss=${T_LOSS[$i]}"
done

log_hierarchy "before_first_step"
detect_htb_netem_parent

prev=0
for i in "${!T_AT[@]}"; do
  at="${T_AT[$i]}"
  delay="${T_DELAY[$i]}"
  loss="${T_LOSS[$i]}"
  sleep_sec=$((at - prev))
  if ((sleep_sec > 0)); then sleep "$sleep_sec"; fi
  log "step $((i + 1))/${#T_AT[@]} at=${at}s delay=${delay} loss=${loss} dev=${IFACE} parent=${NETEM_PARENT}"
  timeline_event tc_step "step=$((i + 1))" "at_s=${at}" "delay=${delay}" "loss=${loss}" "iface=${IFACE}"
  apply_deterioration "$delay" "$loss"
  log_hierarchy "after_step_$((i + 1))"
  prev=$at
done
log "finished all steps"
timeline_event tc_finished "step_count=${#T_AT[@]}" "iface=${IFACE}"
