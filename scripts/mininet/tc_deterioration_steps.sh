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
HTB_RATE=""
CURRENT_STEP=0
COMPLETED=0
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

on_exit() {
  local status=$?
  trap - EXIT
  log "exiting status=${status} current_step=${CURRENT_STEP} completed=${COMPLETED}"
  exit "$status"
}
trap on_exit EXIT

log_tc_output() {
  local prefix="$1"
  shift
  local timeout_sec="${TC_DIAGNOSTIC_TIMEOUT_SEC:-5}"
  local -a cmd=("$@")
  local output="" status=0

  if command -v timeout >/dev/null 2>&1; then
    cmd=(timeout --signal=KILL "${timeout_sec}s" "${cmd[@]}")
  fi
  if output="$("${cmd[@]}" 2>&1)"; then
    status=0
  else
    status=$?
    log "warning: ${prefix} inspection failed status=${status}: ${output}"
  fi
  while IFS= read -r line; do
    if [[ -n "$line" ]]; then
      log "${prefix}: ${line}"
    fi
  done <<< "$output"
  return 0
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
  local -a netem_parents=() netem_handles=() class_ids=() class_leaf_ids=() class_rates=()
  local netem_parent="" netem_handle="" class_parent="" leaf_handle="" class_rate=""
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
    if [[ "$line" =~ rate[[:space:]]+([^[:space:]]+) ]]; then
      class_rates+=("${BASH_REMATCH[1]}")
    else
      class_rates+=("")
    fi
  done < <(tc class show dev "$IFACE" 2>/dev/null | awk '/^class htb / {print}' || true)

  # Keep only classes under the detected HTB major.
  local -a filtered_class_ids=() filtered_leaf_ids=() filtered_class_rates=()
  local idx
  for idx in "${!class_ids[@]}"; do
    if [[ "${class_ids[$idx]}" == "${htb_major}:"* ]]; then
      filtered_class_ids+=("${class_ids[$idx]}")
      filtered_leaf_ids+=("${class_leaf_ids[$idx]}")
      filtered_class_rates+=("${class_rates[$idx]}")
    fi
  done
  class_ids=("${filtered_class_ids[@]}")
  class_leaf_ids=("${filtered_leaf_ids[@]}")
  class_rates=("${filtered_class_rates[@]}")

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
        class_rate="${class_rates[$idx]}"
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
    class_rate="${class_rates[0]}"
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
  HTB_RATE="${class_rate}"

  log "detected_root_htb=${ROOT_HTB_HANDLE} detected_htb_parent=${NETEM_PARENT} detected_netem_handle=${NETEM_HANDLE:-none} detected_htb_rate=${HTB_RATE:-unknown}"
  return 0
}

verify_deterioration() {
  local expected_delay="$1"
  local expected_loss="$2"
  local qdisc_output="" class_output="" root_line="" netem_line="" class_line="" line

  if ! qdisc_output="$(tc qdisc show dev "$IFACE" 2>&1)"; then
    log "verification failed: qdisc inspection failed: ${qdisc_output}"
    return 1
  fi
  if ! class_output="$(tc class show dev "$IFACE" 2>&1)"; then
    log "verification failed: class inspection failed: ${class_output}"
    return 1
  fi
  while IFS= read -r line; do
    if [[ "$line" == qdisc\ htb\ "$ROOT_HTB_HANDLE"* && "$line" == *" root "* ]]; then root_line="$line"; fi
    if [[ "$line" == qdisc\ netem\ * && "$line" == *" parent ${NETEM_PARENT} "* ]]; then netem_line="$line"; fi
  done <<< "$qdisc_output"
  while IFS= read -r line; do
    if [[ "$line" == class\ htb\ "$NETEM_PARENT"* ]]; then class_line="$line"; fi
  done <<< "$class_output"

  [[ -n "$root_line" ]] || { log "verification failed: root HTB ${ROOT_HTB_HANDLE} is absent"; return 1; }
  [[ -n "$class_line" ]] || { log "verification failed: HTB class ${NETEM_PARENT} is absent"; return 1; }
  if [[ -n "$HTB_RATE" && "$class_line" != *" rate ${HTB_RATE} "* ]]; then
    log "verification failed: HTB class rate changed; expected ${HTB_RATE}: ${class_line}"
    return 1
  fi
  [[ -n "$netem_line" ]] || { log "verification failed: netem is not under ${NETEM_PARENT}"; return 1; }
  if [[ "$netem_line" != *" delay ${expected_delay}"* ]]; then
    log "verification failed: expected delay ${expected_delay}: ${netem_line}"
    return 1
  fi
  if [[ "$expected_loss" == "0%" ]]; then
    if [[ "$netem_line" == *" loss "* && "$netem_line" != *" loss 0%"* ]]; then
      log "verification failed: expected zero or omitted loss: ${netem_line}"
      return 1
    fi
  elif [[ "$netem_line" != *" loss ${expected_loss}"* ]]; then
    log "verification failed: expected loss ${expected_loss}: ${netem_line}"
    return 1
  fi
  log "verified root=${ROOT_HTB_HANDLE} parent=${NETEM_PARENT} rate=${HTB_RATE:-unknown} delay=${expected_delay} loss=${expected_loss}"
}

apply_deterioration() {
  local delay="$1"
  local loss="$2"
  local tc_cmd=(tc qdisc replace dev "$IFACE" parent "$NETEM_PARENT")

  if [[ -n "$NETEM_HANDLE" ]]; then
    tc_cmd+=(handle "$NETEM_HANDLE")
  fi
  tc_cmd+=(netem delay "$delay" loss "$loss")

  local tc_output=""
  log "${tc_cmd[*]}"
  if ! tc_output="$("${tc_cmd[@]}" 2>&1)"; then
    echo "tc_deterioration: netem update failed: ${tc_output}" >&2
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
  verify_deterioration "$delay" "$loss"

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
  CURRENT_STEP=$((i + 1))
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
COMPLETED=1
