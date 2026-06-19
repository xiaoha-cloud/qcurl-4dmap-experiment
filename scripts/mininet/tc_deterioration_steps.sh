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
ROOT_HTB_HANDLE=""
NETEM_PARENT=""
NETEM_HANDLE=""
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

log_tc_output() {
  local prefix="$1"
  shift
  local timeout_sec="${TC_DIAGNOSTIC_TIMEOUT_SEC:-5}"
  local -a cmd=("$@")

  if command -v timeout >/dev/null 2>&1; then
    cmd=(timeout --signal=KILL "${timeout_sec}s" "${cmd[@]}")
  fi
  while IFS= read -r line; do
    [[ -n "$line" ]] && log "${prefix}: ${line}"
  done < <("${cmd[@]}" 2>&1 || true)
}

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
  log_tc_output qdisc tc -s qdisc show dev "$IFACE"
  log "=== hierarchy ${label}: class dev=${IFACE} ==="
  log_tc_output class tc -s class show dev "$IFACE"
  log "=== hierarchy ${label}: filter dev=${IFACE} ==="
  log_tc_output filter tc -s filter show dev "$IFACE"
}

log_hierarchy_after_step() {
  local label="$1"
  log "=== hierarchy ${label}: qdisc dev=${IFACE} ==="
  log_tc_output qdisc tc -s qdisc show dev "$IFACE"
  log "=== hierarchy ${label}: class dev=${IFACE} ==="
  log_tc_output class tc -s class show dev "$IFACE"
}

detect_htb_netem_parent() {
  local root_line htb_major
  local -a netem_parents=() netem_handles=() class_ids=() class_leaf_ids=()
  local netem_parent="" netem_handle="" class_parent="" leaf_handle=""
  local uniq_parent_count uniq_class_count

  root_line="$(tc qdisc show dev "$IFACE" 2>/dev/null | awk '/^qdisc htb/ && / root / {print; exit}')"
  if [[ -z "$root_line" ]]; then
    root_line="$(tc qdisc show dev "$IFACE" 2>/dev/null | head -n 1 || true)"
  fi
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
    ROOT_HTB_HANDLE="${htb_major}:"
  else
    echo "tc_deterioration: failed to parse HTB root handle on ${IFACE}" >&2
    echo "tc_deterioration: ${root_line}" >&2
    return 1
  fi

  while IFS= read -r line; do
    [[ "$line" =~ ^qdisc[[:space:]]+netem[[:space:]]+([0-9]+):[[:space:]]+parent[[:space:]]+([0-9]+:[0-9]+) ]] || continue
    netem_handles+=("${BASH_REMATCH[1]}:")
    netem_parents+=("${BASH_REMATCH[2]}")
  done < <(tc qdisc show dev "$IFACE" 2>/dev/null || true)

  if ((${#netem_parents[@]} > 0)); then
    uniq_parent_count="$(printf '%s\n' "${netem_parents[@]}" | sort -u | wc -l | tr -d ' ')"
    if [[ "$uniq_parent_count" != "1" ]]; then
      echo "tc_deterioration: ambiguous netem parents on ${IFACE}; expected one HTB leaf parent" >&2
      printf '%s\n' "${netem_parents[@]}" >&2
      return 1
    fi
    netem_parent="${netem_parents[0]}"
    netem_handle="${netem_handles[0]}"
    if [[ "$netem_parent" != "${htb_major}:"* ]]; then
      echo "tc_deterioration: netem parent ${netem_parent} is not under HTB root ${ROOT_HTB_HANDLE} on ${IFACE}" >&2
      return 1
    fi
  fi

  while IFS= read -r line; do
    [[ "$line" =~ ^class[[:space:]]+htb[[:space:]]+([0-9]+:[0-9]+) ]] || continue
    class_ids+=("${BASH_REMATCH[1]}")
    if [[ "$line" =~ leaf[[:space:]]+([0-9]+): ]]; then
      class_leaf_ids+=("${BASH_REMATCH[1]}:")
    else
      class_leaf_ids+=("")
    fi
  done < <(tc class show dev "$IFACE" 2>/dev/null | awk '/^class htb / {print}' || true)

  # Keep only classes under the detected HTB major.
  local -a filtered_class_ids=() filtered_leaf_ids=()
  local idx
  for idx in "${!class_ids[@]}"; do
    if [[ "${class_ids[$idx]}" == "${htb_major}:"* ]]; then
      filtered_class_ids+=("${class_ids[$idx]}")
      filtered_leaf_ids+=("${class_leaf_ids[$idx]}")
    fi
  done
  class_ids=("${filtered_class_ids[@]}")
  class_leaf_ids=("${filtered_leaf_ids[@]}")

  if ((${#class_ids[@]} == 0)); then
    echo "tc_deterioration: no HTB class ${htb_major}:x found on ${IFACE}" >&2
    return 1
  fi

  uniq_class_count="$(printf '%s\n' "${class_ids[@]}" | sort -u | wc -l | tr -d ' ')"
  if [[ "$uniq_class_count" != "1" && -z "$netem_parent" ]]; then
    echo "tc_deterioration: ambiguous HTB leaf classes on ${IFACE}; multiple ${htb_major}:x without netem parent" >&2
    printf '%s\n' "${class_ids[@]}" >&2
    return 1
  fi

  if [[ -n "$netem_parent" ]]; then
    local found_class=false
    for idx in "${!class_ids[@]}"; do
      if [[ "${class_ids[$idx]}" == "$netem_parent" ]]; then
        found_class=true
        class_parent="${class_ids[$idx]}"
        leaf_handle="${class_leaf_ids[$idx]}"
        break
      fi
    done
    if [[ "$found_class" != true ]]; then
      echo "tc_deterioration: netem parent ${netem_parent} has no matching HTB class on ${IFACE}" >&2
      return 1
    fi
    if [[ -n "$leaf_handle" && -n "$netem_handle" && "$leaf_handle" != "$netem_handle" ]]; then
      echo "tc_deterioration: HTB leaf handle ${leaf_handle} disagrees with netem handle ${netem_handle} on ${IFACE}" >&2
      return 1
    fi
  else
    if ((${#class_ids[@]} > 1)); then
      echo "tc_deterioration: ambiguous HTB leaf classes on ${IFACE}; multiple ${htb_major}:x candidates" >&2
      printf '%s\n' "${class_ids[@]}" >&2
      return 1
    fi
    class_parent="${class_ids[0]}"
    leaf_handle="${class_leaf_ids[0]}"
    if [[ -n "$leaf_handle" ]]; then
      netem_handle="$leaf_handle"
    fi
  fi

  if [[ -n "$netem_parent" && -n "$class_parent" && "$netem_parent" != "$class_parent" ]]; then
    echo "tc_deterioration: detected netem parent ${netem_parent} disagrees with HTB class ${class_parent} on ${IFACE}" >&2
    return 1
  fi

  NETEM_PARENT="${netem_parent:-$class_parent}"
  NETEM_HANDLE="${netem_handle}"

  log "detected_root_htb=${ROOT_HTB_HANDLE} detected_htb_parent=${NETEM_PARENT} detected_netem_handle=${NETEM_HANDLE:-none}"
  return 0
}

apply_deterioration() {
  local delay="$1"
  local loss="$2"
  local tc_cmd=(tc qdisc replace dev "$IFACE" parent "$NETEM_PARENT")

  if [[ -n "$NETEM_HANDLE" ]]; then
    tc_cmd+=(handle "$NETEM_HANDLE")
  fi
  tc_cmd+=(netem delay "$delay" loss "$loss")

  log "${tc_cmd[*]}"
  if "${tc_cmd[@]}" 2>/dev/null; then
    :
  elif tc qdisc add dev "$IFACE" parent "$NETEM_PARENT" netem delay "$delay" loss "$loss" 2>/dev/null; then
    log "tc qdisc add dev ${IFACE} parent ${NETEM_PARENT} netem delay ${delay} loss ${loss}"
  else
    echo "tc_deterioration: failed to attach netem under parent ${NETEM_PARENT} on ${IFACE}" >&2
    return 1
  fi

  if ! tc qdisc show dev "$IFACE" | grep -Eq "^qdisc[[:space:]]+netem[[:space:]]+[^[:space:]]+[[:space:]]+parent[[:space:]]+${NETEM_PARENT}([[:space:]]|$)"; then
    echo "tc_deterioration: netem child not present under ${NETEM_PARENT} after apply on ${IFACE}" >&2
    log_hierarchy "after_failed_apply"
    return 1
  fi
  if tc qdisc show dev "$IFACE" | awk '/^qdisc / {print; exit}' | grep -q "qdisc netem.*root"; then
    echo "tc_deterioration: root became netem on ${IFACE}; HTB hierarchy was destroyed" >&2
    return 1
  fi

  log "applied delay=${delay} loss=${loss} dev=${IFACE} parent=${NETEM_PARENT} handle=${NETEM_HANDLE:-auto}"
  return 0
}

if [[ "${TC_DETERIORATION_DETECT_ONLY:-}" == "1" ]]; then
  log_hierarchy "detect_only"
  detect_htb_netem_parent
  log "detected_root_htb=${ROOT_HTB_HANDLE} detected_htb_parent=${NETEM_PARENT} detected_netem_handle=${NETEM_HANDLE:-none}"
  exit 0
fi

log "profile=${PROFILE}"
log "parsed IFACE=${IFACE} step_count=${#T_AT[@]}"
for i in "${!T_AT[@]}"; do
  log "profile_step[$i] at=${T_AT[$i]}s delay=${T_DELAY[$i]} loss=${T_LOSS[$i]}"
done

log_hierarchy "before_first_step"
detect_htb_netem_parent

steps_start_seconds=$SECONDS
for i in "${!T_AT[@]}"; do
  at="${T_AT[$i]}"
  delay="${T_DELAY[$i]}"
  loss="${T_LOSS[$i]}"
  elapsed=$((SECONDS - steps_start_seconds))
  sleep_sec=$((at - elapsed))
  if ((sleep_sec > 0)); then log "waiting ${sleep_sec}s for step $((i + 1))/${#T_AT[@]}"; fi
  while ((sleep_sec > 0)); do
    if ! sleep "$sleep_sec"; then
      log "sleep interrupted before step $((i + 1))/${#T_AT[@]}; resuming wait"
    fi
    elapsed=$((SECONDS - steps_start_seconds))
    sleep_sec=$((at - elapsed))
  done
  log "step $((i + 1))/${#T_AT[@]} at=${at}s delay=${delay} loss=${loss} dev=${IFACE} parent=${NETEM_PARENT}"
  timeline_event tc_step "step=$((i + 1))" "at_s=${at}" "delay=${delay}" "loss=${loss}" "iface=${IFACE}"
  apply_deterioration "$delay" "$loss"
  log_hierarchy_after_step "after_step_$((i + 1))"
done
log "finished all steps"
timeline_event tc_finished "step_count=${#T_AT[@]}" "iface=${IFACE}"
