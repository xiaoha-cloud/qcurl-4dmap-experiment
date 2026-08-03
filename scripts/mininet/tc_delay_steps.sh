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

ROOT_HTB_HANDLE=""
NETEM_PARENT=""
NETEM_HANDLE=""
NETEM_LIMIT=""
NETEM_LOSS=""
NETEM_JITTER=""
HTB_CLASS_BEFORE=""

log "profile=${PROFILE}"
log "parsed IFACE=${IFACE} step_count=${#T_AT[@]}"
for i in "${!T_AT[@]}"; do
  log "profile_step[$i] at=${T_AT[$i]}s delay=${T_DELAY[$i]}"
done

log_qdisc_state() {
  local label="$1"
  log "=== ${label}: qdisc dev=${IFACE} ==="
  tc -s -d qdisc show dev "$IFACE" 2>&1 | while IFS= read -r line; do
    log "qdisc_stats: ${line}"
  done
  log "=== ${label}: class dev=${IFACE} ==="
  tc class show dev "$IFACE" 2>&1 | while IFS= read -r line; do
    log "class: ${line}"
  done
}

fail_hierarchy() {
  log "error: $*"
  log_qdisc_state "failure_state"
  exit 1
}

detect_target_netem() {
  local qdisc_output root_line netem_count netem_line class_line
  qdisc_output="$(tc qdisc show dev "$IFACE" 2>/dev/null || true)"
  root_line="$(printf '%s\n' "$qdisc_output" | awk '/^qdisc htb/ && / root / {print; exit}')"
  [[ -n "$root_line" ]] || fail_hierarchy "HTB root not found on ${IFACE}; refusing to replace root qdisc"
  [[ "$root_line" =~ qdisc[[:space:]]+htb[[:space:]]+([^[:space:]]+)[[:space:]]+root ]] || fail_hierarchy "failed to parse HTB root handle from: ${root_line}"
  ROOT_HTB_HANDLE="${BASH_REMATCH[1]}"

  netem_count="$(printf '%s\n' "$qdisc_output" | awk '/^qdisc netem / && / parent / {n++} END {print n+0}')"
  [[ "$netem_count" == "1" ]] || fail_hierarchy "expected exactly one child netem qdisc on ${IFACE}, found ${netem_count}"
  netem_line="$(printf '%s\n' "$qdisc_output" | awk '/^qdisc netem / && / parent / {print; exit}')"

  [[ "$netem_line" =~ qdisc[[:space:]]+netem[[:space:]]+([^[:space:]]+) ]] || fail_hierarchy "failed to parse netem handle from: ${netem_line}"
  NETEM_HANDLE="${BASH_REMATCH[1]}"
  [[ "$netem_line" =~ parent[[:space:]]+([^[:space:]]+) ]] || fail_hierarchy "failed to parse netem parent from: ${netem_line}"
  NETEM_PARENT="${BASH_REMATCH[1]}"
  [[ "$NETEM_PARENT" == "${ROOT_HTB_HANDLE%:}:"* ]] || fail_hierarchy "netem parent ${NETEM_PARENT} is not under HTB root ${ROOT_HTB_HANDLE}"

  if [[ "$netem_line" =~ limit[[:space:]]+([0-9]+) ]]; then
    NETEM_LIMIT="${BASH_REMATCH[1]}"
  fi
  if [[ "$netem_line" =~ loss[[:space:]]+([0-9.]+%) ]]; then
    NETEM_LOSS="${BASH_REMATCH[1]}"
  fi
  if [[ "$netem_line" =~ delay[[:space:]]+[^[:space:]]+[[:space:]]+([0-9.]+(ms|us|s)) ]]; then
    NETEM_JITTER="${BASH_REMATCH[1]}"
  fi
  if [[ "$netem_line" == *" duplicate "* || "$netem_line" == *" corrupt "* || "$netem_line" == *" reorder "* ]]; then
    fail_hierarchy "unsupported existing netem options would not be safely preserved: ${netem_line}"
  fi

  class_line="$(tc class show dev "$IFACE" 2>/dev/null | awk -v cls="$NETEM_PARENT" '$0 ~ "^class htb " cls {print; exit}')"
  [[ -n "$class_line" ]] || fail_hierarchy "HTB class ${NETEM_PARENT} not found on ${IFACE}"
  HTB_CLASS_BEFORE="$class_line"

  log "detected_root_htb=${ROOT_HTB_HANDLE} detected_netem_parent=${NETEM_PARENT} detected_netem_handle=${NETEM_HANDLE} netem_limit=${NETEM_LIMIT:-unknown} preserved_loss=${NETEM_LOSS:-none} preserved_jitter=${NETEM_JITTER:-none}"
}

verify_hierarchy() {
  local expected_delay="$1" qdisc_output root_line netem_line class_line
  qdisc_output="$(tc qdisc show dev "$IFACE" 2>/dev/null || true)"
  root_line="$(printf '%s\n' "$qdisc_output" | awk '/^qdisc htb/ && / root / {print; exit}')"
  [[ -n "$root_line" ]] || fail_hierarchy "HTB root disappeared after applying delay=${expected_delay}"
  if printf '%s\n' "$qdisc_output" | awk '/^qdisc / {print; exit}' | grep -q 'qdisc netem.*root'; then
    fail_hierarchy "root became netem after applying delay=${expected_delay}; HTB hierarchy was destroyed"
  fi
  netem_line="$(printf '%s\n' "$qdisc_output" | awk -v parent="$NETEM_PARENT" '$0 ~ "^qdisc netem " && $0 ~ " parent " parent {print; exit}')"
  [[ -n "$netem_line" ]] || fail_hierarchy "netem child under ${NETEM_PARENT} not found after applying delay=${expected_delay}"
  [[ "$netem_line" == *" delay ${expected_delay}"* ]] || fail_hierarchy "expected delay ${expected_delay}, got: ${netem_line}"
  if [[ -n "$NETEM_LIMIT" && "$netem_line" != *" limit ${NETEM_LIMIT}"* ]]; then
    fail_hierarchy "netem limit changed; expected ${NETEM_LIMIT}: ${netem_line}"
  fi
  if [[ -n "$NETEM_LOSS" && "$netem_line" != *" loss ${NETEM_LOSS}"* ]]; then
    fail_hierarchy "netem loss changed; expected ${NETEM_LOSS}: ${netem_line}"
  fi
  if [[ -z "$NETEM_LOSS" && "$netem_line" == *" loss "* ]]; then
    fail_hierarchy "netem loss appeared unexpectedly: ${netem_line}"
  fi
  class_line="$(tc class show dev "$IFACE" 2>/dev/null | awk -v cls="$NETEM_PARENT" '$0 ~ "^class htb " cls {print; exit}')"
  [[ -n "$class_line" ]] || fail_hierarchy "HTB class ${NETEM_PARENT} disappeared after applying delay=${expected_delay}"
  [[ "$class_line" == "$HTB_CLASS_BEFORE" ]] || fail_hierarchy "HTB class changed; before=${HTB_CLASS_BEFORE}; after=${class_line}"
  log "verified root=${ROOT_HTB_HANDLE} parent=${NETEM_PARENT} handle=${NETEM_HANDLE} delay=${expected_delay} limit=${NETEM_LIMIT:-unknown} loss=${NETEM_LOSS:-none} htb_class_preserved=yes"
}

apply_delay() {
  local delay="$1"
  local -a cmd=(tc qdisc replace dev "$IFACE" parent "$NETEM_PARENT" handle "$NETEM_HANDLE" netem)
  if [[ -n "$NETEM_LIMIT" ]]; then
    cmd+=(limit "$NETEM_LIMIT")
  fi
  cmd+=(delay "$delay")
  if [[ -n "$NETEM_JITTER" ]]; then
    cmd+=("$NETEM_JITTER")
  fi
  if [[ -n "$NETEM_LOSS" ]]; then
    cmd+=(loss "$NETEM_LOSS")
  fi
  log "tc_cmd: ${cmd[*]}"
  "${cmd[@]}"
  log "applied delay=${delay} dev=${IFACE} mode=mininet_child parent=${NETEM_PARENT} handle=${NETEM_HANDLE}"
  verify_hierarchy "$delay"
  log_qdisc_state "after_apply_delay_${delay}"
}

log_qdisc_state "before_detect"
detect_target_netem

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
